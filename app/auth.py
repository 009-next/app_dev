"""メールのワンタイムコード認証（MVP。2026-09-19 に決定）。

- コードは HMAC でハッシュ保存し、平文を保存しない。一度きり・有効期限・試行回数の上限。
- 存在しないメールにも同じ形の応答を返す（アカウントの存在を明かさない。N4）。
- 開発モードだけ、コードを応答とログに出す（本番モードは起動を拒否する）。
- セッションは要求のたびに、メンバーが有効かを確認する（停止の即時反映）。
- 再認証（ステップアップ）: 直近 STEPUP_WINDOW 秒以内に、コードを確認した場合だけ通す（N5）。
"""

from __future__ import annotations

import hmac
import logging
import secrets

from . import db

log = logging.getLogger("mirucon.auth")

CODE_TTL = 600        # 値は実装時に決める [未定]。暫定
MAX_ATTEMPTS = 5      # 同上
SESSION_TTL = 12 * 3600
STEPUP_WINDOW = 300
RATE_MAX, RATE_WINDOW = 5, 600

_rate: dict[str, list[float]] = {}


def reset_rate() -> None:
    _rate.clear()


def rate_ok(key: str) -> bool:
    t = db.now()
    hits = [x for x in _rate.get(key, []) if t - x < RATE_WINDOW]
    if len(hits) >= RATE_MAX:
        _rate[key] = hits
        return False
    hits.append(t)
    _rate[key] = hits
    return True


def _gen_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def _hash(conn, purpose: str, target_id: str, code: str) -> str:
    return db.hmac_hex(conn, f"{purpose}:{target_id}:{code}")


def issue_code(conn, purpose: str, target_id: str) -> str:
    db.run(conn, "UPDATE login_code SET used=1 WHERE purpose=? AND target_id=? AND used=0", (purpose, target_id))
    code = _gen_code()
    db.run(conn, "INSERT INTO login_code VALUES(?,?,?,?,?,0,0,?)",
           (db.new_id("code_"), purpose, target_id, _hash(conn, purpose, target_id, code), db.now() + CODE_TTL, db.now()))
    return code


def verify_code(conn, purpose: str, target_id: str, code: str) -> bool:
    row = db.one(conn, "SELECT * FROM login_code WHERE purpose=? AND target_id=? AND used=0 "
                       "ORDER BY created_at DESC LIMIT 1", (purpose, target_id))
    if row is None or row["expires_at"] < db.now():
        return False
    attempts = row["attempts"] + 1
    if attempts > MAX_ATTEMPTS:
        db.run(conn, "UPDATE login_code SET used=1 WHERE code_id=?", (row["code_id"],))
        return False
    db.run(conn, "UPDATE login_code SET attempts=? WHERE code_id=?", (attempts, row["code_id"]))
    ok = hmac.compare_digest(row["code_hash"], _hash(conn, purpose, target_id, str(code)))
    if ok:
        db.run(conn, "UPDATE login_code SET used=1 WHERE code_id=?", (row["code_id"],))
    return ok


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def request_login_code(conn, email: str, ip: str = "", purpose: str = "login") -> str:
    """コードを発行する。存在しないメール・回数超過のときも、同じ形のダミーを返す。"""
    email = normalize_email(email)
    allowed = rate_ok("e:" + email) and rate_ok("i:" + (ip or "-"))
    member = db.one(conn, "SELECT * FROM member WHERE email=? AND status='active'", (email,))
    if member is None or not allowed:
        return _gen_code()  # 有効ではないダミー
    code = issue_code(conn, purpose, member["member_id"])
    log.info("(開発モード) ワンタイムコード email=%s purpose=%s code=%s", email, purpose, code)
    return code


def create_session(conn, kind: str, member_id: str | None = None, invite_id: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    t = db.now()
    db.run(conn, "INSERT INTO session VALUES(?,?,?,?,?,?,NULL,?)",
           (db.sha(token), kind, member_id, invite_id, t + SESSION_TTL, t, t))
    return token


def get_session(conn, token: str | None):
    if not token:
        return None
    row = db.one(conn, "SELECT * FROM session WHERE session_id=?", (db.sha(token),))
    if row is None or row["invalidated_at"] or row["expires_at"] < db.now():
        return None
    if row["kind"] == "member":
        m = db.one(conn, "SELECT status FROM member WHERE member_id=?", (row["member_id"],))
        if m is None or m["status"] != "active":
            return None
    return row


def touch_auth(conn, session_id: str) -> None:
    db.run(conn, "UPDATE session SET last_auth_at=? WHERE session_id=?", (db.now(), session_id))


def recent_auth(row) -> bool:
    return db.now() - row["last_auth_at"] <= STEPUP_WINDOW


def logout(conn, token: str | None) -> None:
    if token:
        db.run(conn, "UPDATE session SET invalidated_at=? WHERE session_id=?", (db.now(), db.sha(token)))


# ---- メンバー管理 ----------------------------------------------------------

def _active_owners(conn, org_id) -> int:
    return db.one(conn, "SELECT COUNT(*) c FROM member WHERE org_id=? AND role='owner' AND status='active'",
                  (org_id,))["c"]


def add_member(conn, org_id: str, email: str, role: str = "member") -> str:
    mid = db.new_id("mem_")
    db.run(conn, "INSERT INTO member VALUES(?,?,?,?, 'active', ?, NULL)", (mid, org_id, normalize_email(email), role, db.now()))
    return mid


def suspend_member(conn, org_id: str, actor_id: str, target_id: str) -> tuple[bool, str]:
    target = db.get_member(conn, org_id, target_id)
    if target is None:
        return False, "見つかりません"
    if target["status"] == "suspended":
        return True, "すでに停止しています"
    if target["role"] == "owner" and _active_owners(conn, org_id) <= 1:
        return False, "最後のオーナーは停止できません"
    db.run(conn, "UPDATE member SET status='suspended', suspended_at=? WHERE member_id=?", (db.now(), target_id))
    db.run(conn, "UPDATE session SET invalidated_at=? WHERE member_id=? AND invalidated_at IS NULL", (db.now(), target_id))
    db.run(conn, "UPDATE object SET registrant_id=? WHERE org_id=? AND registrant_id=?", (actor_id, org_id, target_id))
    db.run(conn, "UPDATE object SET assignee_id=? WHERE org_id=? AND assignee_id=?", (actor_id, org_id, target_id))
    n = db.one(conn, "SELECT COUNT(*) c FROM share WHERE org_id=? AND issuer_id=? AND revoked_at IS NULL AND expires_at>?",
               (org_id, target_id, db.now()))["c"]
    db.audit(conn, org_id, "member.suspend", actor_id, target_id, f"有効な共有リンク {n} 件は自動では取り消していない")
    return True, f"停止しました。この人が発行した有効な共有リンクは {n} 件あります（自動では取り消していません）"


def make_owner(conn, org_id: str, actor_id: str, target_id: str) -> tuple[bool, str]:
    t = db.get_member(conn, org_id, target_id)
    if t is None or t["status"] != "active":
        return False, "見つかりません"
    db.run(conn, "UPDATE member SET role='owner' WHERE member_id=?", (target_id,))
    db.audit(conn, org_id, "owner.add", actor_id, target_id)
    return True, "オーナーを追加しました"


def demote_owner(conn, org_id: str, actor_id: str, target_id: str) -> tuple[bool, str]:
    t = db.get_member(conn, org_id, target_id)
    if t is None or t["role"] != "owner":
        return False, "見つかりません"
    if _active_owners(conn, org_id) <= 1:
        return False, "最後のオーナーは降格できません"
    db.run(conn, "UPDATE member SET role='member' WHERE member_id=?", (target_id,))
    db.audit(conn, org_id, "owner.demote", actor_id, target_id)
    return True, "降格しました"
