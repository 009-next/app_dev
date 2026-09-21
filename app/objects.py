"""物の登録・タグ・履歴の取得。権限の判定は authz.can()、取得は org_id 条件つきの db 関数だけを使う。

- タグの中身は推測できないIDだけ（物の名前・住所・担当者名を書かない）。
- 匿名でタグを読んだ人に見せるのは、物の名前と組織の問い合わせ窓口だけ。
"""

from __future__ import annotations

import datetime as dt
import json

from . import authz, db


class Denied(Exception):
    """権限がない。"""


class NotFound(Exception):
    """存在しない、または見えない（区別しない）。"""


def require(actor: authz.Actor, action: str, resource: dict | None = None) -> None:
    if not authz.can(actor, action, resource):
        raise Denied(action)


def get_object_for(conn, actor: authz.Actor, obj_id: str):
    obj = db.get_object(conn, actor.org_id, obj_id) if actor.org_id else None
    if obj is None:
        raise NotFound(obj_id)
    return obj


def register_object(conn, actor: authz.Actor, name: str, kind: str | None = None,
                    assignee_id: str | None = None, next_check: str | None = None):
    """物を登録し、タグを1つ発行する。(物, タグ) を返す。"""
    require(actor, "register_object", {"org_id": actor.org_id})
    if not (name or "").strip():
        raise ValueError("物の名前が空です")
    if assignee_id is not None and db.get_member(conn, actor.org_id, assignee_id) is None:
        raise NotFound(assignee_id)
    if next_check:
        try:
            dt.date.fromisoformat(next_check)  # 定期の確認（periodic.py）が読める形式だけ保存する
        except ValueError:
            raise ValueError("次回点検日は YYYY-MM-DD の形式で入力してください") from None
    next_check = next_check or None
    obj_id = db.new_id("obj_")
    db.run(conn, "INSERT INTO object(obj_id,org_id,name,kind,registrant_id,assignee_id,next_check,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?)",
           (obj_id, actor.org_id, name.strip(), kind, actor.member_id, assignee_id, next_check, db.now()))
    db.audit(conn, actor.org_id, "object.register", actor.member_id, obj_id)
    tag = issue_tag(conn, actor, obj_id)
    conn.commit()
    return db.get_object(conn, actor.org_id, obj_id), tag


def issue_tag(conn, actor: authz.Actor, obj_id: str):
    obj = get_object_for(conn, actor, obj_id)
    require(actor, "register_object", dict(obj))
    tag_id = db.new_token("t_")  # タグの中身。推測できない値
    db.run(conn, "INSERT INTO tag(tag_id,obj_id,org_id,issuer_id,created_at) VALUES(?,?,?,?,?)",
           (tag_id, obj_id, actor.org_id, actor.member_id, db.now()))
    db.audit(conn, actor.org_id, "tag.issue", actor.member_id, obj_id)
    return db.one(conn, "SELECT * FROM tag WHERE tag_id=?", (tag_id,))


def disable_tag(conn, actor: authz.Actor, tag_id: str) -> None:
    tag = db.one(conn, "SELECT * FROM tag WHERE org_id=? AND tag_id=?", (actor.org_id, tag_id))
    if tag is None:
        raise NotFound("tag")
    obj = get_object_for(conn, actor, tag["obj_id"])
    require(actor, "disable_tag", dict(obj))
    db.run(conn, "UPDATE tag SET disabled_at=? WHERE tag_id=? AND disabled_at IS NULL", (db.now(), tag_id))
    db.audit(conn, actor.org_id, "tag.disable", actor.member_id, obj["obj_id"])
    conn.commit()


def resolve_tag(conn, tag_id: str, actor: authz.Actor | None = None) -> dict | None:
    """タグを読んだ結果。存在しない・無効なタグは区別なく None。
    匿名・他組織には名前と問い合わせ窓口だけ。同じ組織のメンバーには obj_id も返す。"""
    tag = db.one(conn, "SELECT * FROM tag WHERE tag_id=?", (tag_id or "",))
    if tag is None or tag["disabled_at"]:
        return None
    obj = db.get_object(conn, tag["org_id"], tag["obj_id"])
    org = db.get_org(conn, tag["org_id"])
    if obj is None or org is None:
        return None
    out = {"name": obj["name"], "contact": org["contact"]}
    if actor is not None and actor.kind == "member" and actor.org_id == tag["org_id"]:
        out["obj_id"] = obj["obj_id"]
    # 利用履歴（ポートキー）。読んだ人を特定する情報（IP・端末）は残さない。匿名は anon とだけ記録する
    who = actor.member_id if (actor is not None and actor.kind == "member" and actor.org_id == tag["org_id"]) else "anon"
    db.audit(conn, tag["org_id"], "tag.read", who, tag["tag_id"], "")
    conn.commit()
    return out


def card_resource(conn, card) -> dict:
    """権限判定に渡すカードの情報。登録者は物から得る。"""
    res = dict(card)
    obj = db.get_object(conn, card["org_id"], card["obj_id"])
    res["registrant_id"] = obj["registrant_id"] if obj else None
    res["watcher_member_ids"] = [r["member_id"] for r in db.many(
        conn, "SELECT member_id FROM card_watcher WHERE card_id=? AND removed_at IS NULL AND member_id IS NOT NULL",
        (card["card_id"],))]
    res["watcher_invite_ids"] = [r["invite_id"] for r in db.many(
        conn, "SELECT invite_id FROM card_watcher WHERE card_id=? AND removed_at IS NULL AND invite_id IS NOT NULL",
        (card["card_id"],))]
    return res


def object_history(conn, actor: authz.Actor, obj_id: str) -> list:
    """物のカード一覧（古い順）。メンバーは見られるカードだけ。AI は、その物の履歴を読める。"""
    obj = get_object_for(conn, actor, obj_id)
    cards = db.cards_of_object(conn, actor.org_id, obj_id)
    if actor.kind == "ai":
        require(actor, "read_object_history", dict(obj))
        return cards
    return [c for c in cards if authz.can(actor, "view_card", card_resource(conn, c))]


GAP_DAYS = 90  # これ以上あいた期間は「記録なし」として時系列に挟む


def timeline(conn, actor: authz.Actor, obj_id: str) -> list[dict]:
    """物の時系列（サイコメトリー）。カード・AIの判断を古い順に並べ、記録のない期間を挟む。

    - 各行は、必ず元になった記録の ID を持つ（根拠のない行を混ぜない）。
    - 見られないカードと、そのカードについての判断は出さない（公開範囲を混ぜない）。
    """
    obj = get_object_for(conn, actor, obj_id)
    rows: list[dict] = []
    visible = {c["card_id"]: c for c in object_history(conn, actor, obj_id)}
    for c in visible.values():
        rows.append({"kind": "card", "at": c["created_at"], "id": c["card_id"],
                     "label": c["title"] or "（無題のカード）", "card_type": c["card_type"] or ""})
    can_see_decisions = authz.can(actor, "view_decisions", {"org_id": obj["org_id"], "registrant_id": obj["registrant_id"]})
    if can_see_decisions:
        for d in db.many(conn, "SELECT * FROM decision_log WHERE org_id=? AND obj_id=? ORDER BY created_at",
                         (actor.org_id, obj_id)):
            if d["card_id"] and d["card_id"] not in visible:
                continue  # 見られないカードについての判断は出さない
            rows.append({"kind": "decision", "at": d["created_at"], "id": d["dec_id"], "label": d["chosen_tool"] or "",
                         "stage": d["stage"], "row": d})
    rows.sort(key=lambda r: r["at"])
    out: list[dict] = []
    for i, r in enumerate(rows):
        if i and (r["at"] - rows[i - 1]["at"]) >= GAP_DAYS * 86400.0:
            days = int((r["at"] - rows[i - 1]["at"]) / 86400.0)
            out.append({"kind": "gap", "at": rows[i - 1]["at"] + 1, "id": None, "days": days,
                        "label": f"記録なし（{days}日）"})
        out.append(r)
    return out


def object_spend(conn, org_id: str, obj_id: str) -> float:
    """この物の判断で使った原価の合計（忍びの地図の「予算使用量」）。"""
    r = db.one(conn, "SELECT COALESCE(SUM(cost_usd),0) AS c FROM decision_log WHERE org_id=? AND obj_id=?", (org_id, obj_id))
    return float(r["c"] or 0.0)


def tags_of(conn, org_id: str, obj_id: str) -> list[dict]:
    """有効なタグと、最後に読まれた日時（ポートキーの利用履歴）。"""
    out = []
    for t in db.many(conn, "SELECT * FROM tag WHERE org_id=? AND obj_id=? AND disabled_at IS NULL", (org_id, obj_id)):
        last = db.one(conn, "SELECT MAX(at) AS at FROM audit_log WHERE org_id=? AND action='tag.read' AND target=?",
                      (org_id, t["tag_id"]))
        out.append({"tag_id": t["tag_id"], "last_read_at": last["at"] if last else None})
    return out


def next_actions(conn, actor: authz.Actor, obj_id: str, *, client_factory=None, config: dict | None = None) -> dict:
    """次に取りうる行動の選択肢を、AI に比べさせる（D7・フォース・ビジョン）。

    先に確信度（記録の件数と新しさ）をコードで確かめ、足りなければ **LLM を呼ばずに** 追加の確認事項を返す。
    案は「条件付きの比較」であって、断定でも作業の指示でもない。入力にない数値・型番を含む案は、コードが拒否する。
    """
    from . import agent, signals, summaries

    obj = get_object_for(conn, actor, obj_id)
    require(actor, "create_edit_card", {"org_id": obj["org_id"]})
    shown = summaries.eligible_cards(conn, actor.org_id, obj_id)[-summaries.MAX_CARDS:]
    conf = signals.confidence(shown)
    note_list = signals.notes(conn, actor.org_id, obj_id)
    if conf["label"] != "usable":
        # 確信度が足りないので、予測しない。何を足せば判断できるかだけを返す（費用は発生しない）
        return {"confidence": conf, "options": [], "refused": "",
                "questions": ["この物の、日時の入った最近の記録を追加してください（現在の状態・点検の結果）。"]
                             + [n["message"] for n in note_list]}

    inputs = {"物": obj["name"]}
    if obj["summary"] and obj["summary_status"] in ("current", "held"):
        inputs["現在の要約"] = obj["summary"]
    sources = [{"field": k, "text": v, "source_card_id": None} for k, v in inputs.items()]
    for c in shown:
        when = dt.date.fromtimestamp(c["created_at"]).isoformat()
        body = "\n".join(x for x in (c["title"], f"作業前: {c['before_desc']}" if c["before_desc"] else "",
                                     f"作業後: {c['after_desc']}" if c["after_desc"] else "") if x)[:summaries.MAX_TEXT]
        key = f"カード {c['card_id']}（{when}・{c['card_type'] or ''}）"
        inputs[key] = body
        sources.append({"field": key, "text": body, "source_card_id": c["card_id"]})

    ctx = agent.RunCtx()
    ctx.valid_card_ids = {c["card_id"] for c in shown}
    dec = agent.decide(conn, actor.org_id, stage="options", inputs=inputs, obj_id=obj_id, trigger="next_actions",
                       sources=sources, ctx=ctx, client_factory=client_factory, config=config,
                       notes=note_list, eligible_cards=len(shown))
    options = []
    if dec.applied and dec.tool == "propose_next_actions" and not dec.default_used:
        options = [{k: str(o[k])[:300] for k in agent.OPTION_KEYS} for o in dec.args["options"]]
        db.run(conn, "UPDATE object SET next_options=?, next_options_at=? WHERE org_id=? AND obj_id=?",
               (json.dumps({"options": options, "evidence_card_ids": dec.args["evidence_card_ids"],
                            "confidence": conf, "reason": dec.reason}, ensure_ascii=False),
                str(db.now()), actor.org_id, obj_id))
        db.audit(conn, actor.org_id, "object.next_actions", actor.member_id, obj_id, f"{len(options)}件の案")
        conn.commit()
    return {"confidence": conf, "options": options, "refused": dec.default_reason if not options else "",
            "questions": [] if options else [n["message"] for n in note_list], "cost_usd": dec.cost_usd or 0.0}


def last_options_refusal(conn, org_id: str, obj_id: str) -> str:
    """直近の「次の行動の案」の判断が、検査で拒否されていたら、その理由。なければ空文字。"""
    d = db.one(conn, "SELECT chosen_tool, validation FROM decision_log WHERE org_id=? AND obj_id=? AND stage='options' "
                     "ORDER BY created_at DESC LIMIT 1", (org_id, obj_id))
    if d is None or d["chosen_tool"] != "no_action":
        return ""
    return (d["validation"] or "").replace("既定動作: ", "")


def stored_options(conn, org_id: str, obj_id: str) -> dict | None:
    """保存済みの将来シナリオ。後から新しいカードが増えていれば stale=True。"""
    obj = db.get_object(conn, org_id, obj_id)
    if obj is None or not obj["next_options"]:
        return None
    data = json.loads(obj["next_options"])
    made_at = float(obj["next_options_at"] or 0)
    evidence_ids = [str(card_id) for card_id in data.get("evidence_card_ids", []) if isinstance(card_id, str)]
    # 時計が粗い環境でも、案を作った直後と同じ時刻の新規カードを見落とさない。
    # 根拠として使ったカードは除外し、比較後に追加されたカードだけで再作成を促す。
    placeholders = ",".join("?" for _ in evidence_ids)
    excluded = f" AND card_id NOT IN ({placeholders})" if placeholders else ""
    newer = db.one(conn, "SELECT COUNT(*) AS n FROM card WHERE org_id=? AND obj_id=? AND deleted_at IS NULL "
                   f"AND created_at>=?{excluded}", (org_id, obj_id, made_at, *evidence_ids))
    data["made_at"] = made_at
    data["stale"] = bool(newer and newer["n"])
    return data
