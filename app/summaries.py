"""物の「経緯と今の状態」の要約（判断 D4）。カードを追加したときだけ更新を判断する（閲覧のたびには呼ばない）。

- 根拠にするのは、組織のメンバー全員が見られるカード（link_30d・org_only）だけ。招待限定のカードは、
  その内容を見られないメンバーにも要約が見えてしまうので、AI に見せない。
- update_summary は、AI に見せたカードの ID を根拠に持たなければ、適用しない（agent._guard）。
- 保留（hold_summary）・矛盾の通知案（draft_notification）は、要約を更新せず、既存の要約に「保留中」の印を付ける。
- 失敗・境界違反・上限到達は、既定動作（要約を更新せず保留）。要約がまだなければ、何も変えない。
- 根拠のカードが削除された・招待限定に狭められたときは、要約を隠す（invalidate_for_card）。
"""

from __future__ import annotations

import datetime as dt
import json

from . import agent, db, notifications, signals

ELIGIBLE_SCOPES = ("link_30d", "org_only")
MAX_CARDS = 8
MAX_TEXT = 300
MAX_SUMMARY = 800


def eligible_cards(conn, org_id: str, obj_id: str) -> list:
    return [c for c in db.cards_of_object(conn, org_id, obj_id) if c["scope"] in ELIGIBLE_SCOPES]


def refresh(conn, org_id: str, obj_id: str, *, ctx: "agent.RunCtx | None" = None, client_factory=None,
            config: dict | None = None, trigger: str = "card_create", skip_title_of: str | None = None,
            notes: list[dict] | None = None):
    """要約の更新を AI に判断させ、結果を適用する。判断（Decision）を返す。根拠にできるカードがなければ None（LLM は呼ばない）。
    skip_title_of: そのカードのタイトルを入力に含めない（並列で作成中のカードは、タイトルがまだ空で、説明文の生成物でもあるため）。"""
    obj = db.get_object(conn, org_id, obj_id)
    shown = eligible_cards(conn, org_id, obj_id)[-MAX_CARDS:]
    if obj is None or not shown:
        return None
    inputs = {"物": obj["name"]}
    if obj["summary"] and obj["summary_status"] in ("current", "held"):
        inputs["現在の要約"] = obj["summary"]
    sources = [{"field": k, "text": v, "source_card_id": None} for k, v in inputs.items()]
    for c in shown:
        when = dt.date.fromtimestamp(c["created_at"]).isoformat()
        body = "\n".join(x for x in (None if c["card_id"] == skip_title_of else c["title"], f"作業前: {c['before_desc']}" if c["before_desc"] else "",
                                     f"作業後: {c['after_desc']}" if c["after_desc"] else "") if x)[:MAX_TEXT]
        key = f"カード {c['card_id']}（{when}・{c['card_type'] or ''}）"
        inputs[key] = body
        sources.append({"field": key, "text": body, "source_card_id": c["card_id"]})
    ctx = ctx or agent.RunCtx()
    ctx.valid_card_ids = {c["card_id"] for c in shown}  # 根拠にできるのは、AI に見せたカードだけ
    dec = agent.decide(conn, org_id, stage="summary", inputs=inputs, obj_id=obj_id, card_id=None, trigger=trigger,
                       sources=sources, ctx=ctx, client_factory=client_factory, config=config,
                       recipient_ok=notifications.recipient_ok_for(conn, org_id, obj_id),
                       notes=signals.notes(conn, org_id, obj_id) if notes is None else notes, eligible_cards=len(shown))
    _apply(conn, org_id, obj, dec)
    if dec.applied:
        notifications.save_extra_drafts(conn, org_id, obj_id, dec)  # 同じ応答の矛盾の通知案（G-1）
    conn.commit()
    return dec


def _apply(conn, org_id: str, obj, dec: "agent.Decision") -> None:
    if not dec.applied:  # 判断ログを保存できなかった判断は適用しない
        return
    if dec.tool == "update_summary" and not dec.default_used:
        text = str(dec.args.get("summary", "")).strip()[:MAX_SUMMARY]
        if text:
            db.run(conn, "UPDATE object SET summary=?, summary_status='current', summary_sources=? WHERE org_id=? AND obj_id=?",
                   (text, json.dumps(dec.args["evidence_card_ids"]), org_id, obj["obj_id"]))
            db.audit(conn, org_id, "summary.update", "ai", obj["obj_id"], f"根拠{len(dec.args['evidence_card_ids'])}件")
            return
    if dec.tool == "draft_notification":
        notifications.save_draft(conn, org_id, obj["obj_id"], dec)
    if dec.tool in ("hold_summary", "draft_notification") and obj["summary"]:
        db.run(conn, "UPDATE object SET summary_status='held' WHERE org_id=? AND obj_id=? AND summary_status='current'",
               (org_id, obj["obj_id"]))


def invalidate_for_card(conn, org_id: str, card) -> None:
    """根拠のカードが削除された・招待限定に狭められたら、要約を隠す（次のカード追加で作り直す）。"""
    obj = db.get_object(conn, org_id, card["obj_id"])
    if obj is None or not obj["summary_sources"]:
        return
    if card["card_id"] in json.loads(obj["summary_sources"]):
        db.run(conn, "UPDATE object SET summary=NULL, summary_status='stale', summary_sources=NULL WHERE org_id=? AND obj_id=?",
               (org_id, obj["obj_id"]))
        db.audit(conn, org_id, "summary.invalidate", "system", obj["obj_id"], f"根拠のカード {card['card_id']} が使えなくなった")
