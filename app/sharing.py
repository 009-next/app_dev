"""共有リンク・招待・署名付きの短命な画像URL（N6）。

- 共有リンクは、共有範囲が link_30d のカードだけに発行できる。範囲を狭めたら、リンクは働かなくなる。
- 画像のURLは、署名・期限（短命）を持ち、配信のたびに、共有・招待が生きているかを確認する
  （取り消し後は、画像も見られない）。
- トークン・署名はログに出さない。
"""

from __future__ import annotations

import hmac

from . import db

DAY = 86400
IMG_URL_TTL = 300


def create_share(conn, org_id: str, card, issuer_id: str, approver_id: str | None, ttl_days: int = 30):
    share_id, token = db.new_id("shr_"), db.new_token("s_")
    db.run(conn, "INSERT INTO share VALUES(?,?,?,?,?,?,?,NULL,0,?)",
           (share_id, token, card["card_id"], org_id, issuer_id, approver_id,
            db.now() + max(1, min(ttl_days, 30)) * DAY, db.now()))
    db.audit(conn, org_id, "share.issue", issuer_id, card["card_id"], f"share={share_id} 期限={ttl_days}日")
    return db.one(conn, "SELECT * FROM share WHERE share_id=?", (share_id,))


def resolve_share(conn, token: str):
    """有効な共有なら (share, card)。存在しない・期限切れ・取り消し・範囲外は、区別なく None。"""
    share = db.one(conn, "SELECT * FROM share WHERE token=?", (token or "",))
    if share is None or share["revoked_at"] or share["expires_at"] < db.now():
        return None
    card = db.get_card(conn, share["org_id"], share["card_id"])
    if card is None or card["scope"] != "link_30d":
        return None
    return share, card


def share_alive(conn, share_id: str, card_id: str) -> bool:
    share = db.one(conn, "SELECT * FROM share WHERE share_id=?", (share_id,))
    if share is None or share["revoked_at"] or share["expires_at"] < db.now() or share["card_id"] != card_id:
        return False
    card = db.get_card(conn, share["org_id"], card_id)
    return card is not None and card["scope"] == "link_30d"


def revoke_share(conn, org_id: str, share_id: str, actor_id: str) -> bool:
    cur = db.run(conn, "UPDATE share SET revoked_at=? WHERE org_id=? AND share_id=? AND revoked_at IS NULL",
                 (db.now(), org_id, share_id))
    if cur.rowcount:
        db.audit(conn, org_id, "share.revoke", actor_id, share_id)
    return bool(cur.rowcount)


def revoke_all_by_member(conn, org_id: str, member_id: str, actor_id: str) -> int:
    cur = db.run(conn, "UPDATE share SET revoked_at=? WHERE org_id=? AND issuer_id=? AND revoked_at IS NULL",
                 (db.now(), org_id, member_id))
    db.audit(conn, org_id, "share.revoke_all", actor_id, member_id, f"{cur.rowcount} 件")
    return cur.rowcount


# ---- 招待 -------------------------------------------------------------------

def create_invite(conn, org_id: str, card, issuer_id: str, email: str, ttl_days: int = 7, watch: bool = False):
    invite_id, token = db.new_id("inv_"), db.new_token("i_")
    db.run(conn, "INSERT INTO invite VALUES(?,?,?,?,?,?,?,?,NULL,?)",
           (invite_id, token, card["card_id"], org_id, email.strip().lower(), issuer_id, 1 if watch else 0,
            db.now() + max(1, min(ttl_days, 14)) * DAY, db.now()))
    db.audit(conn, org_id, "invite.create", issuer_id, card["card_id"], f"invite={invite_id} watch={int(watch)}")
    if watch:
        db.run(conn, "INSERT INTO card_watcher VALUES(?,?,?,NULL,?,?,?,NULL)",
               (db.new_id("wat_"), card["card_id"], org_id, invite_id, issuer_id, db.now()))
    return db.one(conn, "SELECT * FROM invite WHERE invite_id=?", (invite_id,))


def resolve_invite(conn, token: str):
    inv = db.one(conn, "SELECT * FROM invite WHERE token=?", (token or "",))
    if inv is None or inv["revoked_at"] or inv["expires_at"] < db.now():
        return None
    if db.get_card(conn, inv["org_id"], inv["card_id"]) is None:
        return None
    return inv


def invite_alive(conn, invite_id: str, card_id: str) -> bool:
    inv = db.one(conn, "SELECT * FROM invite WHERE invite_id=?", (invite_id,))
    return bool(inv and not inv["revoked_at"] and inv["expires_at"] >= db.now() and inv["card_id"] == card_id
                and db.get_card(conn, inv["org_id"], card_id) is not None)


def revoke_invite(conn, org_id: str, invite_id: str, actor_id: str) -> bool:
    cur = db.run(conn, "UPDATE invite SET revoked_at=? WHERE org_id=? AND invite_id=? AND revoked_at IS NULL",
                 (db.now(), org_id, invite_id))
    if cur.rowcount:
        db.audit(conn, org_id, "invite.revoke", actor_id, invite_id)
    return bool(cur.rowcount)


# ---- 署名付きの画像URL -----------------------------------------------------

def _sig(conn, image_id: str, subject: str, exp: int) -> str:
    return db.hmac_hex(conn, f"img:{image_id}:{subject}:{exp}")


def sign_image(conn, image_id: str, subject: str, ttl: int = IMG_URL_TTL) -> str:
    """subject は 'share:<id>' か 'invite:<id>'。"""
    exp = int(db.now() + ttl)
    return f"/img/{image_id}?u={subject}&e={exp}&g={_sig(conn, image_id, subject, exp)}"


def verify_image_request(conn, image, subject: str, exp: str, sig: str) -> bool:
    """署名・期限に加え、共有／招待が今も生きているかを確認する。すべての失敗は区別なく False。"""
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_i < db.now() or not hmac.compare_digest(sig or "", _sig(conn, image["image_id"], subject or "", exp_i)):
        return False
    kind, _, ident = (subject or "").partition(":")
    if kind == "share":
        return share_alive(conn, ident, image["card_id"])
    if kind == "invite":
        return invite_alive(conn, ident, image["card_id"])
    return False
