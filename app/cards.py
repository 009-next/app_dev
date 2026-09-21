"""カードの作成・取得・共有範囲の変更・削除・共有リンクの発行。

- 権限は authz.can()。共有リンクの発行・共有範囲の拡大・削除は、作り手の操作としてだけ実装する
  （AI には渡さない。agent.py の STAGE_TOOLS にも入れない）。
- カード作成の最後に物エージェントを呼ぶ。AI の選択は「提案・下書き」として保存し、確定は作り手が行う。
- 画像は、処理（EXIF 除去・ぼかし）が終わってから保存する。失敗したら何も残さない。
"""

from __future__ import annotations

import json
import os
import pathlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout

from . import agent, authz, db, images, llm, sharing, signals, summaries
from .objects import Denied, NotFound, card_resource, get_object_for, require

DATA_DIR = pathlib.Path(__file__).with_name("data") / "images"
DEFAULT_SCOPE = "link_30d"  # CLAUDE.md: 既定は「リンクを知っている人のみ・期限付き」。リンクの発行は別の操作


class NotReady(Exception):
    """共有できる状態ではない（理由をメッセージに持つ）。"""


def _get(conn, actor: authz.Actor, card_id: str):
    card = db.get_card(conn, actor.org_id, card_id) if actor.org_id else None
    if card is None:
        raise NotFound(card_id)
    return card


def get_card(conn, actor: authz.Actor, card_id: str):
    """見られないカードは、存在しないカードと区別しない。"""
    card = _get(conn, actor, card_id)
    if not authz.can(actor, "view_card", card_resource(conn, card)):
        raise NotFound(card_id)
    return card


# 写真の出どころ。通話の画面には、相手の顔・名前・チャットなど第三者の情報が写るので、ほかと区別する
PHOTO_SOURCES = ("camera", "video_frame", "call_screen")
CALL_SCREEN = "call_screen"


def _sources(images_in) -> list[str]:
    """images_in の4つ目（出どころ）。知らない値・未指定は camera として扱う。"""
    out = []
    for item in images_in:
        src = item[3] if len(item) > 3 else "camera"
        out.append(src if src in PHOTO_SOURCES else "camera")
    return out


def _open_ratios(images_in, sources) -> list[float | None]:
    """images_in の5つ目（端末が測った、開けた範囲の割合）。通話の画面だけ・0〜1 の数だけ受け取る。それ以外は None（従来どおり）。"""
    out = []
    for item, src in zip(images_in, sources):
        v = item[4] if len(item) > 4 else None
        ok = src == CALL_SCREEN and isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= float(v) <= 1.0
        out.append(float(v) if ok else None)
    return out


def locate_image(stored: str) -> pathlib.Path:
    """保存された画像のパスを、実在する場所に解決する。保存時の絶対パスがあればそのまま（従来どおり）。
    フォルダを移した・別の場所へコピーしたときは、今の DATA_DIR の同名ファイルを探す（画像ファイルは <image_id>.jpg）。"""
    p = pathlib.Path(stored)
    if p.is_file():
        return p
    alt = DATA_DIR / p.name
    return alt if alt.is_file() else p


def _save_card(conn, actor: authz.Actor, obj_id: str, *, before_desc: str, after_desc: str, voice_text: str, images_in,
               scope: str, mask_confirmed: bool, data_dir) -> str:
    """カードと画像を保存する（LLM は呼ばない。速い）。card_id を返す。"""
    obj = get_object_for(conn, actor, obj_id)
    require(actor, "create_edit_card", dict(obj))
    if scope not in authz.SCOPES:
        raise ValueError(f"共有範囲が正しくありません: {scope}")
    sources = _sources(images_in)
    ratios = _open_ratios(images_in, sources)
    processed = [(role, images.process(item[1], item[2])) for item in images_in for role in [item[0]]]  # 失敗したら何も書かない
    forced = ""
    if CALL_SCREEN in sources and scope != "invited_only":
        # 通話の画面には第三者が写る。作り手が広い範囲を選んでいても、コードが最も狭い範囲にする
        forced, scope = scope, "invited_only"

    card_id = db.new_id("card_")
    db.run(conn, "INSERT INTO card(card_id,obj_id,org_id,creator_id,card_type,scope,before_desc,after_desc,voice_text,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)",
           (card_id, obj_id, actor.org_id, actor.member_id, "generic", scope,
            before_desc, after_desc, voice_text, db.now()))
    out_dir = pathlib.Path(data_dir) if data_dir else DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    for (role, data), src, ratio in zip(processed, sources, ratios):
        image_id = db.new_id("img_")
        path = out_dir / f"{image_id}.jpg"
        path.write_bytes(data)
        db.run(conn, "INSERT INTO image(image_id,card_id,org_id,role,path,mask_confirmed,created_at,source,open_ratio) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
               (image_id, card_id, actor.org_id, role, str(path), int(mask_confirmed), db.now(), src, ratio))
    db.audit(conn, actor.org_id, "card.create", actor.member_id, card_id, f"画像{len(processed)}枚")
    if forced:
        db.audit(conn, actor.org_id, "scope.forced", "system", card_id,
                 f"通話の画面から取り込んだ写真があるため、{forced} から invited_only に狭めた")
    conn.commit()
    return card_id


def create_card(conn, actor: authz.Actor, obj_id: str, *, before_desc: str = "", after_desc: str = "",
                voice_text: str = "", images_in=(), scope: str = DEFAULT_SCOPE, mask_confirmed: bool = False,
                client_factory=None, config: dict | None = None, data_dir=None) -> dict:
    """images_in は [(role, JPEG のバイト列, ぼかし領域の一覧 or None)]。role は 'before' / 'after' / 'other'。
    戻り値: {"card": 行, "decisions": [...], "cost_usd": 合計}。AI の処理が終わってから返す（同期）。"""
    card_id = _save_card(conn, actor, obj_id, before_desc=before_desc, after_desc=after_desc, voice_text=voice_text,
                         images_in=images_in, scope=scope, mask_confirmed=mask_confirmed, data_dir=data_dir)
    result = _run_agent(conn, actor, card_id, client_factory, config)
    return {"card": db.get_card(conn, actor.org_id, card_id), **result}


def create_card_async(conn, actor: authz.Actor, obj_id: str, *, jobs, before_desc: str = "", after_desc: str = "",
                      voice_text: str = "", images_in=(), scope: str = DEFAULT_SCOPE, mask_confirmed: bool = False,
                      client_factory=None, config: dict | None = None, data_dir=None) -> dict:
    """カードを保存したらすぐ返し、AI の処理（分類・共有・説明文・要約）は jobs に任せる。素材がなければ、AI は動かさない。
    AI の状態は card.ai_status（running → done / failed）。分類・共有・説明文は、jobs.parallel なら同時に実行する。"""
    card_id = _save_card(conn, actor, obj_id, before_desc=before_desc, after_desc=after_desc, voice_text=voice_text,
                         images_in=images_in, scope=scope, mask_confirmed=mask_confirmed, data_dir=data_dir)
    if before_desc or after_desc or voice_text:
        db.run(conn, "UPDATE card SET ai_status='running' WHERE card_id=?", (card_id,))
        conn.commit()
        jobs.submit(conn, lambda job_conn: _ai_job(job_conn, actor, card_id, client_factory, config, jobs.parallel))
    return {"card": db.get_card(conn, actor.org_id, card_id), "async": True}


class AIDeadline(Exception):
    """AI の処理全体の期限（config["job_deadline"]）を超えた。出た結果は保存済みで、残りは待たない。"""


def _ai_job(conn, actor, card_id, client_factory, config, parallel) -> None:
    try:
        _run_agent(conn, actor, card_id, client_factory, config, parallel=parallel)
        status = "done"
    except AIDeadline as e:
        status = "timeout"
        db.audit(conn, actor.org_id, "card.ai_timeout", "system", card_id, str(e)[:200])
    except Exception as e:  # 想定外の失敗。カードは作成済みで、AI の結果だけが付かない
        status = "failed"
        db.audit(conn, actor.org_id, "card.ai_failed", "system", card_id, f"{type(e).__name__}: {str(e)[:200]}")
    db.run(conn, "UPDATE card SET ai_status=? WHERE card_id=?", (status, card_id))
    conn.commit()


def _apply_classify(conn, card_id, d1, skip_scope: bool = False) -> None:
    """skip_scope: 共有の段階がすでに範囲の提案を保存したとき、分類の提案で上書きしない（順番に実行するときと同じ結果にする）。"""
    if d1.applied:
        if d1.tool == "select_card_type":
            db.run(conn, "UPDATE card SET card_type=? WHERE card_id=?", (d1.args["type_id"], card_id))
        elif d1.tool == "request_more_input":
            db.run(conn, "UPDATE card SET pending_question=? WHERE card_id=?", (str(d1.args["question"])[:300], card_id))
        elif d1.tool == "propose_narrower_scope" and not skip_scope:
            db.run(conn, "UPDATE card SET proposed_scope=? WHERE card_id=?", (d1.args["scope"], card_id))


def _apply_privacy(conn, card_id, d2) -> None:
    if d2.applied and d2.tool == "propose_narrower_scope":
        db.run(conn, "UPDATE card SET proposed_scope=? WHERE card_id=?", (d2.args["scope"], card_id))
    if d2.applied and d2.tool == "propose_extra_mask":
        # 提案として保存し、カード画面に出す（適用は作り手）。最新の提案で置き換える
        db.run(conn, "UPDATE card SET proposed_mask=? WHERE card_id=?",
               (json.dumps({"target": str(d2.args.get("target", ""))[:200], "reason": d2.reason[:300]}, ensure_ascii=False), card_id))


def _apply_text(conn, card_id, text) -> None:
    if text:
        db.run(conn, "UPDATE card SET ai_text=?, title=COALESCE(NULLIF(title,''),?) WHERE card_id=?",
               (json.dumps(text, ensure_ascii=False), text["title"], card_id))


def _run_parallel(conn, actor, card_id, inputs, common, client_factory, config, deadline: float | None = None,
                  with_summary: bool = False, notes: list[dict] | None = None):
    """分類・共有・説明文は、互いの結果を使わないので、同時に実行する。with_summary のときは、物の要約の更新（D4）も
    4 つ目として同時に走らせる（要約の入力は、カードの作業前後の文章で足り、タイトルを使わない）。判断ごとに別の RunCtx を使い、あとで合算する。
    結果は、揃うのを待たず、出たものから順に保存する（説明文が先に出れば、タイトルが先に付く。共有の判断が
    切り替えで長引いても、待たされない）。途中で失敗しても、保存済みの結果は残る。"""
    c1, c2, c3, c4 = agent.RunCtx(), agent.RunCtx(), agent.RunCtx(), agent.RunCtx()
    sconn = db.serialized(conn)  # スレッドが同じ接続を使う。保存も、この入れ物を通す（db.LockedConn）
    done, privacy_scope = {}, False
    ex = ThreadPoolExecutor(max_workers=4 if with_summary else 3)
    try:
        futs = {ex.submit(agent.decide, sconn, actor.org_id, stage="classify", inputs=inputs, **{**common, "ctx": c1}): "classify",
                ex.submit(agent.decide, sconn, actor.org_id, stage="privacy", inputs=inputs, **{**common, "ctx": c2}): "privacy",
                ex.submit(agent.describe, sconn, actor.org_id, inputs, c3, client_factory, config): "text"}
        if with_summary:
            futs[ex.submit(summaries.refresh, sconn, actor.org_id, common["obj_id"], ctx=c4, client_factory=client_factory,
                           config=config, skip_title_of=card_id, notes=notes)] = "summary"
        try:
            for fut in as_completed(futs, timeout=deadline):
                kind, res = futs[fut], fut.result()
                if kind == "summary":
                    pass  # summaries.refresh が、自分で適用・保存する
                elif kind == "text":
                    _apply_text(sconn, card_id, res)
                elif kind == "classify":
                    _apply_classify(sconn, card_id, res, skip_scope=privacy_scope)
                else:
                    _apply_privacy(sconn, card_id, res)
                    privacy_scope = privacy_scope or (res.applied and res.tool == "propose_narrower_scope")
                sconn.commit()
                done[kind] = res
        except FuturesTimeout:
            raise AIDeadline(f"{deadline:.0f}秒以内に終わらなかった判断: " + ", ".join(sorted({"classify", "privacy", "text"} | ({"summary"} if with_summary else set()) - set(done)))) from None
    finally:
        ex.shutdown(wait=False, cancel_futures=True)  # 期限を超えても、走っている呼び出しを待たない（呼び出し自体にもタイムアウトがある）
    return agent.RunCtx(calls=c1.calls + c2.calls + c3.calls + c4.calls, llm_calls=c1.llm_calls + c2.llm_calls + c3.llm_calls + c4.llm_calls,
                        questions=c1.questions, cost=c1.cost + c2.cost + c3.cost + c4.cost,
                        decisions=[done["classify"], done["privacy"]] + ([done["summary"]] if done.get("summary") else []))


def _run_agent(conn, actor, card_id, client_factory, config, parallel: bool = False) -> dict:
    card = db.get_card(conn, actor.org_id, card_id)
    inputs = {k: v for k, v in {"作業前の説明": card["before_desc"], "作業後の説明": card["after_desc"],
                                "吹き込み": card["voice_text"]}.items() if v}
    # 出どころを伝える。AI は写真そのものを見ないので、通話の画面かどうかは文章で知らせるしかない
    if db.one(conn, "SELECT 1 AS x FROM image WHERE card_id=? AND source=?", (card_id, CALL_SCREEN)):
        inputs[signals.CALL_SCREEN_KEY] = ("ビデオ通話の画面から取り込んだ1コマが含まれます。"
                                    "画面のほとんどは隠してあり、作り手が残す所だけを開けています。")
    ctx = agent.RunCtx()
    if not inputs:  # 素材がなければ LLM を呼ばない
        return {"decisions": [], "cost_usd": 0.0}
    # 必要の部屋の材料（コードだけで分かること）。画像のないカードに、ぼかしの提案は渡さない
    has_images = bool(db.one(conn, "SELECT 1 AS x FROM image WHERE card_id=?", (card_id,)))
    notes = signals.notes(conn, actor.org_id, card["obj_id"])
    common = dict(obj_id=card["obj_id"], card_id=card_id, current_scope=card["scope"],
                  client_factory=client_factory, config=config, ctx=ctx,
                  notes=notes, has_images=has_images)

    summary_done = False
    if parallel:
        cfg = config or llm.load_config()
        summary_done = card["scope"] in summaries.ELIGIBLE_SCOPES
        ctx = _run_parallel(conn, actor, card_id, inputs, common, client_factory, config, deadline=float(cfg.get("job_deadline", 120.0)),
                            with_summary=summary_done, notes=notes)
    else:
        d1 = agent.decide(conn, actor.org_id, stage="classify", inputs=inputs, **common)
        _apply_classify(conn, card_id, d1)
        d2 = agent.decide(conn, actor.org_id, stage="privacy", inputs=inputs, **common)
        _apply_privacy(conn, card_id, d2)
        _apply_text(conn, card_id, agent.describe(conn, actor.org_id, inputs, ctx, client_factory, config))
    conn.commit()
    if not summary_done and card["scope"] in summaries.ELIGIBLE_SCOPES:  # 招待限定のカードは、要約の根拠にならないので、LLM を呼ばない
        summaries.refresh(conn, actor.org_id, card["obj_id"], ctx=ctx, client_factory=client_factory, config=config, notes=notes)
    return {"decisions": ctx.decisions, "cost_usd": ctx.cost}


# ---- 共有範囲 ----------------------------------------------------------------

def narrow_scope(conn, actor: authz.Actor, card_id: str, new_scope: str) -> None:
    """共有範囲を狭める（作り手の操作。AI の提案の適用もこれ）。"""
    card = _get(conn, actor, card_id)
    require(actor, "narrow_scope", card_resource(conn, card))
    if not authz.scope_is_narrower(new_scope, card["scope"]):
        raise ValueError("現在より狭い範囲ではありません")
    db.run(conn, "UPDATE card SET scope=?, proposed_scope=NULL WHERE card_id=?", (new_scope, card_id))
    db.audit(conn, actor.org_id, "scope.narrow", actor.member_id, card_id, f"{card['scope']} -> {new_scope}")
    if new_scope not in summaries.ELIGIBLE_SCOPES:
        summaries.invalidate_for_card(conn, actor.org_id, card)
    conn.commit()


def dismiss_mask_proposal(conn, actor: authz.Actor, card_id: str) -> None:
    """AI の追加のぼかしの提案を、作り手が確認した（対応した、または不要と判断した）として消す。記録は監査ログに残す。"""
    card = _get(conn, actor, card_id)
    require(actor, "create_edit_card", dict(get_object_for(conn, actor, card["obj_id"])))
    if not card["proposed_mask"]:
        raise ValueError("提案がありません")
    db.run(conn, "UPDATE card SET proposed_mask=NULL WHERE card_id=?", (card_id,))
    db.audit(conn, actor.org_id, "mask.proposal_ack", actor.member_id, card_id, card["proposed_mask"][:200])
    conn.commit()


def add_mask_rects(conn, actor: authz.Actor, card_id: str, image_id: str, rects) -> None:
    """保存済みの写真に、モザイクの範囲を追加する（[x, y, w, h] の比率）。元の写真は保存していないので、今の画像に重ねる。
    AI の追加のぼかしの提案が残っていれば、対応したものとして消す。"""
    card = _get(conn, actor, card_id)
    require(actor, "create_edit_card", dict(get_object_for(conn, actor, card["obj_id"])))
    img = db.get_image(conn, actor.org_id, image_id)
    if img is None or img["card_id"] != card_id:
        raise NotFound(image_id)
    path = locate_image(img["path"])
    if not path.is_file():
        raise NotFound(image_id)
    data = images.mosaic(path.read_bytes(), rects)  # 検証・変換に失敗したら ImageError。ファイルは変えない
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)  # 途中で失敗しても、半端なファイルを配らない
    n = len(rects)
    db.audit(conn, actor.org_id, "mask.add", actor.member_id, image_id, f"{n}か所")
    if card["proposed_mask"]:
        db.run(conn, "UPDATE card SET proposed_mask=NULL WHERE card_id=?", (card_id,))
        db.audit(conn, actor.org_id, "mask.proposal_applied", actor.member_id, card_id, card["proposed_mask"][:200])
    conn.commit()


def apply_proposed_scope(conn, actor: authz.Actor, card_id: str) -> None:
    card = _get(conn, actor, card_id)
    if not card["proposed_scope"]:
        raise ValueError("提案がありません")
    narrow_scope(conn, actor, card_id, card["proposed_scope"])


def widen_scope(conn, actor: authz.Actor, card_id: str, new_scope: str) -> None:
    """共有範囲を広げる。オーナーだけ。メンバーは request_widen_scope で申請する。"""
    card = _get(conn, actor, card_id)
    require(actor, "widen_scope", card_resource(conn, card))
    if new_scope not in authz.SCOPES or authz.scope_is_narrower(new_scope, card["scope"]) or new_scope == card["scope"]:
        raise ValueError("現在より広い範囲ではありません")
    db.run(conn, "UPDATE card SET scope=?, proposed_scope=NULL WHERE card_id=?", (new_scope, card_id))
    db.audit(conn, actor.org_id, "scope.widen", actor.member_id, card_id, f"{card['scope']} -> {new_scope}")
    conn.commit()


def request_widen_scope(conn, actor: authz.Actor, card_id: str, new_scope: str) -> str:
    card = _get(conn, actor, card_id)
    require(actor, "request_widen_scope", card_resource(conn, card))
    if new_scope not in authz.SCOPES or authz.scope_is_narrower(new_scope, card["scope"]) or new_scope == card["scope"]:
        raise ValueError("現在より広い範囲ではありません")
    approval_id = db.new_id("apr_")
    db.run(conn, "INSERT INTO approval(approval_id,org_id,kind,card_id,requested_by,payload,created_at) "
                 "VALUES(?,?,?,?,?,?,?)",
           (approval_id, actor.org_id, "widen_scope", card_id, actor.member_id, new_scope, db.now()))
    db.audit(conn, actor.org_id, "scope.widen_request", actor.member_id, card_id, new_scope)
    conn.commit()
    return approval_id


def decide_widen_request(conn, actor: authz.Actor, approval_id: str, approve: bool) -> None:
    """オーナーが申請を承認・却下する。承認すると範囲を広げる。"""
    ap = db.one(conn, "SELECT * FROM approval WHERE org_id=? AND approval_id=? AND kind='widen_scope'",
                (actor.org_id, approval_id))
    if ap is None or ap["status"] != "pending":
        raise NotFound(approval_id)
    card = _get(conn, actor, ap["card_id"])
    require(actor, "widen_scope", card_resource(conn, card))
    if approve:
        widen_scope(conn, actor, ap["card_id"], ap["payload"])
    db.run(conn, "UPDATE approval SET status=?, decided_by=? WHERE approval_id=?",
           ("approved" if approve else "rejected", actor.member_id, approval_id))
    db.audit(conn, actor.org_id, "scope.widen_decide", actor.member_id, approval_id, "approve" if approve else "reject")
    conn.commit()


# ---- 削除・共有 ----------------------------------------------------------------

def delete_card(conn, actor: authz.Actor, card_id: str) -> None:
    """論理削除。共有・招待は、カードが見つからなくなるので働かなくなる。"""
    card = _get(conn, actor, card_id)
    require(actor, "delete_card", card_resource(conn, card))
    db.run(conn, "UPDATE card SET deleted_at=? WHERE card_id=?", (db.now(), card_id))
    summaries.invalidate_for_card(conn, actor.org_id, card)
    db.audit(conn, actor.org_id, "card.delete", actor.member_id, card_id)
    conn.commit()


def issue_share(conn, actor: authz.Actor, card_id: str, *, confirmed: bool, ttl_days: int = 30):
    """共有リンクの発行。作り手が確認画面で承認したとき（confirmed=True）だけ。"""
    card = _get(conn, actor, card_id)
    require(actor, "issue_share", card_resource(conn, card))
    if not confirmed:
        raise NotReady("確認画面の承認がありません")
    if card["scope"] != "link_30d":
        raise NotReady("共有範囲が link_30d のカードだけ、リンクを発行できます")
    unconfirmed = db.one(conn, "SELECT COUNT(*) c FROM image WHERE card_id=? AND mask_confirmed=0", (card_id,))["c"]
    if unconfirmed:
        raise NotReady(f"ぼかしの確認が済んでいない画像が {unconfirmed} 枚あります")
    share = sharing.create_share(conn, actor.org_id, card, actor.member_id, actor.member_id, ttl_days)
    conn.commit()
    return share


__all__ = ["Denied", "NotFound", "NotReady", "create_card", "get_card", "narrow_scope", "apply_proposed_scope",
           "widen_scope", "request_widen_scope", "decide_widen_request", "delete_card", "issue_share"]
