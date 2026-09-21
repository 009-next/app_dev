"""通知の下書きの保存と、人による承認・却下。

- AI が作るのは案（draft_notification）だけ。承認しても、ここでは送信しない
  （メール配信サービスは未決。外部サービスの追加は事前承認が必要）。
- 宛先は、オーナーまたはその物の担当者だけ（agent の draft_notification の enum）。組織外の宛先は扱わない。
- 承認・却下できるのは、その物の登録者とオーナー（権限マトリクスの send_notification）。
"""

from __future__ import annotations

import hmac
import re

from . import agent, authz, db
from .objects import NotFound, require

MAX_MESSAGE = 1000
RECIPIENTS = tuple(agent.SPEC["tools"]["draft_notification"]["input_schema"]["properties"]["recipient"]["enum"])
OWNER, ASSIGNEE = "オーナー", "担当者"


class StaleDraft(Exception):
    """承認しようとした内容が、画面に出したものと違う（画面が古い）。"""


def _operation_id(conn, org_id: str, obj_id: str, dec: "agent.Decision", recipient: str, message: str) -> str:
    """同じ判断から同じ宛先・本文の下書きを作り直しても、増やさないための鍵。"""
    return db.hmac_hex(conn, f"ntf:{org_id}:{obj_id}:{dec.dec_id}:{recipient}:{message}")


def draft_digest(conn, n) -> str:
    """下書きの中身の指紋。承認を、画面に出した本文そのものに紐づけるために使う。"""
    return db.hmac_hex(conn, f"ntfd:{n['notif_id']}:{n['recipient']}:{n['message']}")


def _obj_resource(obj) -> dict:
    return {"org_id": obj["org_id"], "registrant_id": obj["registrant_id"]}


def recipient_ok_for(conn, org_id: str, obj_id: str):
    """agent.decide の recipient_ok に渡す検査。宛先が見られないカードの内容を、通知案に含めない。
    オーナーは組織のすべてのカードを見られる。担当者は、招待限定のカードを、作成者・登録者でない限り見られない。"""
    obj = db.get_object(conn, org_id, obj_id)

    def phrases(card) -> set[str]:
        texts = [card["title"], card["before_desc"], card["after_desc"]]
        return {p.strip() for t in texts if t for p in re.split(r"[。．.\n、,]", t) if len(p.strip()) >= 6}

    def ok(recipient: str, message: str) -> bool:
        if recipient == OWNER:
            return True
        if recipient != ASSIGNEE or obj is None or not obj["assignee_id"]:
            return False
        for c in db.cards_of_object(conn, org_id, obj_id):
            if c["scope"] == "invited_only" and obj["assignee_id"] not in (c["creator_id"], obj["registrant_id"]):
                if any(p in message for p in phrases(c)):
                    return False
        return True

    return ok


def save_draft(conn, org_id: str, obj_id: str, dec: "agent.Decision") -> str | None:
    """draft_notification の判断を、下書きとして保存する。保存しないときは None。"""
    if dec.tool != "draft_notification" or not dec.applied:
        return None
    obj = db.get_object(conn, org_id, obj_id)
    recipient = dec.args.get("recipient")
    message = str(dec.args.get("message", "")).strip()[:MAX_MESSAGE]
    if obj is None or recipient not in RECIPIENTS or not message:
        return None
    member_id = None
    if recipient == ASSIGNEE:
        assignee = db.get_member(conn, org_id, obj["assignee_id"]) if obj["assignee_id"] else None
        if assignee is None or assignee["status"] != "active":
            return None  # 宛先がいない案は作らない
        member_id = assignee["member_id"]
    op = _operation_id(conn, org_id, obj_id, dec, recipient, message)
    same = db.one(conn, "SELECT notif_id FROM notification WHERE org_id=? AND operation_id=? AND status='draft'", (org_id, op))
    if same is not None:
        return same["notif_id"]  # 同じ操作の再試行。二重に登録しない
    nid = db.new_id("ntf_")
    db.run(conn, "INSERT INTO notification(notif_id,org_id,obj_id,recipient,recipient_member_id,message,status,dec_id,operation_id,created_at) "
                 "VALUES(?,?,?,?,?,?,'draft',?,?,?)",
           (nid, org_id, obj_id, recipient, member_id, message, dec.dec_id, op, db.now()))
    db.audit(conn, org_id, "notification.draft", "ai", nid, f"物={obj_id}")
    conn.commit()
    return nid


def save_extra_drafts(conn, org_id: str, obj_id: str, dec: "agent.Decision") -> list[str]:
    """同じ応答の、ほかのツール呼び出し（G-1）のうち、実行してよいと判定された draft_notification を、下書きとして保存する。"""
    import dataclasses
    ids = []
    for x in dec.extras:
        if x["tool"] == "draft_notification" and x["executed"]:
            nid = save_draft(conn, org_id, obj_id, dataclasses.replace(dec, tool="draft_notification", args=x["args"]))
            if nid:
                ids.append(nid)
    return ids


def list_drafts(conn, actor: authz.Actor, status: str = "draft") -> list:
    """承認できる下書きだけ（オーナー: 組織のすべて／メンバー: 自分が登録した物）。"""
    if not actor.org_id:
        return []
    rows = db.many(conn, "SELECT n.*, o.name AS obj_name, o.registrant_id AS registrant_id, o.org_id AS o_org "
                         "FROM notification n JOIN object o ON o.obj_id=n.obj_id AND o.org_id=n.org_id "
                         "WHERE n.org_id=? AND n.status=? ORDER BY n.created_at", (actor.org_id, status))
    return [r for r in rows if authz.can(actor, "send_notification", {"org_id": r["org_id"], "registrant_id": r["registrant_id"]})]


def decide_draft(conn, actor: authz.Actor, notif_id: str, approve: bool, digest: str | None = None) -> None:
    """承認は status を approved にするだけ（送信はしない）。決定済みの下書きは、存在しないものとして扱う。

    digest を渡すと、画面に出した本文と一致することを確かめてから承認する（一致しなければ StaleDraft）。
    渡さなければ従来どおり。
    """
    n = db.one(conn, "SELECT * FROM notification WHERE org_id=? AND notif_id=?", (actor.org_id, notif_id)) if actor.org_id else None
    obj = db.get_object(conn, actor.org_id, n["obj_id"]) if n else None
    if n is None or obj is None or n["status"] != "draft":
        raise NotFound(notif_id)
    require(actor, "send_notification", _obj_resource(obj))
    if digest is not None and not hmac.compare_digest(digest, draft_digest(conn, n)):
        raise StaleDraft(notif_id)
    db.run(conn, "UPDATE notification SET status=? WHERE notif_id=?", ("approved" if approve else "rejected", notif_id))
    db.audit(conn, actor.org_id, "notification.approve" if approve else "notification.reject", actor.member_id, notif_id,
             f"内容の指紋 {draft_digest(conn, n)[:16]}")
    conn.commit()
