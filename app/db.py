"""SQLite スキーマと共通の補助関数。

取得・更新は、必ず org_id を条件に含める（テナント分離。N8）。
公開のトークン（タグ・共有・招待）で引く場合だけ、トークンから org_id を得る。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time

_OFFSET = 0.0


def now() -> float:
    return time.time() + _OFFSET


def shift_clock(seconds: float) -> None:
    """テスト用: 時計を進める。"""
    global _OFFSET
    _OFFSET += seconds


def reset_clock() -> None:
    global _OFFSET
    _OFFSET = 0.0


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS org(org_id TEXT PRIMARY KEY, name TEXT NOT NULL, contact TEXT NOT NULL, created_at REAL);
CREATE TABLE IF NOT EXISTS member(
  member_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL CHECK(role IN ('owner','member')),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended')),
  created_at REAL, suspended_at REAL);
CREATE TABLE IF NOT EXISTS login_code(
  code_id TEXT PRIMARY KEY, purpose TEXT NOT NULL, target_id TEXT NOT NULL, code_hash TEXT NOT NULL,
  expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, used INTEGER NOT NULL DEFAULT 0, created_at REAL);
CREATE TABLE IF NOT EXISTS session(
  session_id TEXT PRIMARY KEY, kind TEXT NOT NULL, member_id TEXT, invite_id TEXT,
  expires_at REAL NOT NULL, last_auth_at REAL NOT NULL, invalidated_at REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS object(
  obj_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT,
  registrant_id TEXT NOT NULL, assignee_id TEXT, next_check TEXT,
  summary TEXT, summary_status TEXT NOT NULL DEFAULT 'none', summary_sources TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS tag(
  tag_id TEXT PRIMARY KEY, obj_id TEXT NOT NULL, org_id TEXT NOT NULL, issuer_id TEXT NOT NULL,
  created_at REAL, disabled_at REAL);
CREATE TABLE IF NOT EXISTS card(
  card_id TEXT PRIMARY KEY, obj_id TEXT NOT NULL, org_id TEXT NOT NULL, creator_id TEXT NOT NULL,
  card_type TEXT, scope TEXT NOT NULL DEFAULT 'org_only', proposed_scope TEXT,
  title TEXT, before_desc TEXT, after_desc TEXT, voice_text TEXT, values_json TEXT,
  ai_text TEXT, pending_question TEXT, created_at REAL, deleted_at REAL);
CREATE TABLE IF NOT EXISTS image(
  image_id TEXT PRIMARY KEY, card_id TEXT NOT NULL, org_id TEXT NOT NULL, role TEXT NOT NULL,
  path TEXT NOT NULL, mask_confirmed INTEGER NOT NULL DEFAULT 0, created_at REAL);
CREATE TABLE IF NOT EXISTS share(
  share_id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, card_id TEXT NOT NULL, org_id TEXT NOT NULL,
  issuer_id TEXT NOT NULL, approver_id TEXT, expires_at REAL NOT NULL, revoked_at REAL,
  view_count INTEGER NOT NULL DEFAULT 0, created_at REAL);
CREATE TABLE IF NOT EXISTS invite(
  invite_id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, card_id TEXT NOT NULL, org_id TEXT NOT NULL,
  email TEXT NOT NULL, issuer_id TEXT NOT NULL, watch INTEGER NOT NULL DEFAULT 0,
  expires_at REAL NOT NULL, revoked_at REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS card_watcher(
  watcher_id TEXT PRIMARY KEY, card_id TEXT NOT NULL, org_id TEXT NOT NULL,
  member_id TEXT, invite_id TEXT, added_by TEXT, added_at REAL, removed_at REAL);
CREATE TABLE IF NOT EXISTS approval(
  approval_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, kind TEXT NOT NULL, card_id TEXT,
  requested_by TEXT NOT NULL, payload TEXT, status TEXT NOT NULL DEFAULT 'pending',
  decided_by TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS notification(
  notif_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, obj_id TEXT, recipient TEXT NOT NULL,
  recipient_member_id TEXT, message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
  dec_id TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS audit_log(
  audit_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, action TEXT NOT NULL, actor TEXT,
  target TEXT, detail TEXT, at REAL);
CREATE TABLE IF NOT EXISTS decision_log(
  dec_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, obj_id TEXT, card_id TEXT, stage TEXT NOT NULL,
  trigger TEXT, observed TEXT, options TEXT, chosen_tool TEXT, chosen_args TEXT,
  reason TEXT, evidence TEXT, evidence_ok INTEGER, single_source INTEGER,
  validation TEXT, llm_id TEXT, provider TEXT, model TEXT, cost_usd REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS llm_call(
  llm_id TEXT PRIMARY KEY, org_id TEXT, purpose TEXT, provider TEXT, req_model TEXT, resolved_model TEXT,
  in_tok INTEGER, out_tok INTEGER, cache_read INTEGER, cache_write INTEGER,
  cost_usd REAL, cost_status TEXT, status TEXT, error TEXT, latency_s REAL, created_at REAL);
"""


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class _Result:
    """LockedConn.execute の結果。行を、ロックの中で取り出し済み（あとから取り出すと、別スレッドの実行と重なるため）。"""

    def __init__(self, rows, rowcount, lastrowid):
        self._rows, self.rowcount, self.lastrowid = list(rows), rowcount, lastrowid
        self._i = 0

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self):
        rest, self._i = self._rows[self._i:], len(self._rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


class LockedConn:
    """複数のスレッドが、1つの接続を使うときの安全な入れ物。実行と結果の取り出しを、1つのロックの中で行う。
    （素の sqlite3 の接続を、同じ文で同時に使うと、InterfaceError などで壊れる。実測: 並列のカード作成で約25%）
    AI の判断を並列に走らせる間だけ使う。ふだんの処理は、素の接続のまま。"""

    def __init__(self, conn):
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, sql, args=()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            return _Result(cur.fetchall() if cur.description is not None else [], cur.rowcount, cur.lastrowid)

    def executemany(self, sql, seq):
        with self._lock:
            cur = self._conn.executemany(sql, seq)
            return _Result([], cur.rowcount, cur.lastrowid)

    def executescript(self, script):
        with self._lock:
            return self._conn.executescript(script)

    def commit(self):
        with self._lock:
            return self._conn.commit()

    def rollback(self):
        with self._lock:
            return self._conn.rollback()

    def close(self):
        with self._lock:
            return self._conn.close()

    def __getattr__(self, name):  # row_factory など
        return getattr(self._conn, name)


def serialized(conn):
    return conn if isinstance(conn, LockedConn) else LockedConn(conn)


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # 列の追加（古い DB にも足す）。card.proposed_mask: AI が提案した追加のぼかし（JSON。作り手が確認するまで残る）。
    # card.ai_status: AI の処理の状態（running / done / failed。同期で作ったカードは NULL）
    have = {r["name"] for r in conn.execute("PRAGMA table_info(card)")}
    for col in ("proposed_mask", "ai_status"):
        if col not in have:
            conn.execute(f"ALTER TABLE card ADD COLUMN {col} TEXT")
    # decision_log の追加列: evidence_source=根拠がどの資料か（JSON）、risk=危険度、room_reasons=渡さなかったツールと理由（JSON）
    # org_setting.external_llm: 1=Claude 以外の外部モデルへ、条件つきで文章を渡してよい／0=渡さない。行がなければオン（既定）。
    # org に列を足さず別の表にしたのは、org へ位置指定で INSERT している既存のコード・テストを壊さないため。
    conn.execute("CREATE TABLE IF NOT EXISTS org_setting(org_id TEXT PRIMARY KEY, external_llm INTEGER NOT NULL DEFAULT 1)")
    # org_setting.vision_llm: 1=通話の画面（フィルター後）を、作り手の操作で AI に見せてよい／0=見せない。既定はオフ（行がなければ 0）。
    have = {r["name"] for r in conn.execute("PRAGMA table_info(org_setting)")}
    if "vision_llm" not in have:
        conn.execute("ALTER TABLE org_setting ADD COLUMN vision_llm INTEGER NOT NULL DEFAULT 0")
    # org_setting.sensitive_industry: 1=医療・介護など機微な現場（オーナーが宣言）。通話画面を AI に見せる機能を止める。
    # AI の「機微な場面」の判断は、画像の品質で外れる（実測: 劣化すると見逃した）ので、組織の設定で決める。
    have = {r["name"] for r in conn.execute("PRAGMA table_info(org_setting)")}
    if "sensitive_industry" not in have:
        conn.execute("ALTER TABLE org_setting ADD COLUMN sensitive_industry INTEGER NOT NULL DEFAULT 0")
    # org_setting.talk_llm / talk_audio: 会話から次を考える機能／音声分析（音声を音声対応モデルへ渡す第2の経路）。どちらも既定オフ。
    have = {r["name"] for r in conn.execute("PRAGMA table_info(org_setting)")}
    for col in ("talk_llm", "talk_audio"):
        if col not in have:
            conn.execute(f"ALTER TABLE org_setting ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    # talk_*: 会話の文字（音声は保存しない）・作り手が確認した区切り・AI の提案と作り手の応答。保持期間を過ぎたら削除（talk.purge_old）
    conn.execute("CREATE TABLE IF NOT EXISTS talk_session(session_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, obj_id TEXT NOT NULL, "
                 "actor_id TEXT NOT NULL, created_at REAL, source TEXT, consent_ack INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'open', "
                 "audio_pending INTEGER NOT NULL DEFAULT 0, audio_chunks INTEGER NOT NULL DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS talk_segment(seg_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL, who TEXT, "
                 "text TEXT NOT NULL DEFAULT '', reviewed INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0, created_at REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS talk_plan(plan_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, org_id TEXT NOT NULL, created_at REAL, "
                 "model TEXT, kind TEXT, result TEXT, status TEXT NOT NULL DEFAULT 'running', feedback TEXT, iterations INTEGER DEFAULT 0, "
                 "trace TEXT, cost_usd REAL DEFAULT 0, executed TEXT, parent_plan_id TEXT, mode TEXT)")
    # card_vision: AI が、フィルター後の画像を見て出した分析（提案）。result は検査を通ったものだけ（不採用なら NULL）
    conn.execute("CREATE TABLE IF NOT EXISTS card_vision(vision_id TEXT PRIMARY KEY, card_id TEXT NOT NULL, image_id TEXT NOT NULL, "
                 "org_id TEXT NOT NULL, created_at REAL, model TEXT, llm_id TEXT, result TEXT, why TEXT, actor_id TEXT, status TEXT NOT NULL DEFAULT 'running')")
    # org_setting.fusion_demo / fusion_share_dir: 統合分析（音声×共有画面→資料・メール下書き。デモ向け）と、共有先のフォルダ。既定はオフ・未設定。
    have = {r["name"] for r in conn.execute("PRAGMA table_info(org_setting)")}
    if "fusion_demo" not in have:
        conn.execute("ALTER TABLE org_setting ADD COLUMN fusion_demo INTEGER NOT NULL DEFAULT 0")
    if "fusion_share_dir" not in have:
        conn.execute("ALTER TABLE org_setting ADD COLUMN fusion_share_dir TEXT")
    # fusion_run: 統合分析 1 回分（音声から得た文字・作り手が確認した文字・AI の結果）。fusion_file: 生成したファイル（表・文書・赤丸つきの画像）。
    # 音声は保存しない。保持期間を過ぎたら削除（fusion.purge_old）
    conn.execute("CREATE TABLE IF NOT EXISTS fusion_run(fusion_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, card_id TEXT NOT NULL, image_id TEXT NOT NULL, "
                 "actor_id TEXT, created_at REAL, status TEXT NOT NULL DEFAULT 'transcribing', transcript TEXT, confirmed_at REAL, result TEXT, why TEXT, "
                 "model TEXT, model_audio TEXT, cost_usd REAL DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS fusion_file(fusion_id TEXT NOT NULL, name TEXT NOT NULL, mime TEXT, data BLOB, PRIMARY KEY(fusion_id, name))")
    # member_pref: メンバー自身の設定。gmail=送信の準備（Gmail の作成画面）で宛先・アカウントに使う、本人の Gmail アドレス（任意）
    conn.execute("CREATE TABLE IF NOT EXISTS member_pref(member_id TEXT PRIMARY KEY, gmail TEXT)")
    # image.source: その写真の出どころ（camera / video_frame / call_screen）。通話の画面は第三者が写るため区別する
    have = {r["name"] for r in conn.execute("PRAGMA table_info(image)")}
    if "source" not in have:
        conn.execute("ALTER TABLE image ADD COLUMN source TEXT NOT NULL DEFAULT 'camera'")
    # image.open_ratio: 作り手が端末で「開けた範囲」の面積の割合（0〜1）。拡張モードの通話画面だけ（旧来・古い画像は NULL）。
    # 端末が、なぞった形から正確に測った値。サーバーは、そのまま信じず、画素の見積もりと突き合わせる（vision.check）
    if "open_ratio" not in have:
        conn.execute("ALTER TABLE image ADD COLUMN open_ratio REAL")
    # object.next_options: 将来シナリオの案（JSON）と、作った日時
    have = {r["name"] for r in conn.execute("PRAGMA table_info(object)")}
    for col in ("next_options", "next_options_at"):
        if col not in have:
            conn.execute(f"ALTER TABLE object ADD COLUMN {col} TEXT")
    # notification.operation_id: 同じ内容の再試行で二重に登録しないための鍵
    have = {r["name"] for r in conn.execute("PRAGMA table_info(notification)")}
    if "operation_id" not in have:
        conn.execute("ALTER TABLE notification ADD COLUMN operation_id TEXT")
    have = {r["name"] for r in conn.execute("PRAGMA table_info(decision_log)")}
    for col in ("evidence_source", "risk", "room_reasons"):
        if col not in have:
            conn.execute(f"ALTER TABLE decision_log ADD COLUMN {col} TEXT")
    if not conn.execute("SELECT 1 FROM meta WHERE key='secret'").fetchone():
        conn.execute("INSERT INTO meta(key,value) VALUES('secret', ?)", (secrets.token_hex(32),))
    conn.commit()


def one(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def many(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def run(conn, sql, args=()):
    return conn.execute(sql, args)


def new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(8)


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(24)


def secret(conn) -> bytes:
    return bytes.fromhex(one(conn, "SELECT value FROM meta WHERE key='secret'")["value"])


def hmac_hex(conn, msg: str) -> str:
    return hmac.new(secret(conn), msg.encode(), hashlib.sha256).hexdigest()


def sha(msg: str) -> str:
    return hashlib.sha256(msg.encode()).hexdigest()


def audit(conn, org_id, action, actor="", target="", detail="") -> None:
    run(conn, "INSERT INTO audit_log VALUES(?,?,?,?,?,?,?)",
        (new_id("aud_"), org_id, action, actor, target, detail, now()))


# ---- 組織条件つきの取得 ----------------------------------------------------

def get_object(conn, org_id, obj_id):
    return one(conn, "SELECT * FROM object WHERE org_id=? AND obj_id=?", (org_id, obj_id))


def get_card(conn, org_id, card_id):
    return one(conn, "SELECT * FROM card WHERE org_id=? AND card_id=? AND deleted_at IS NULL", (org_id, card_id))


def cards_of_object(conn, org_id, obj_id):
    return many(conn, "SELECT * FROM card WHERE org_id=? AND obj_id=? AND deleted_at IS NULL ORDER BY created_at",
                (org_id, obj_id))


def get_member(conn, org_id, member_id):
    return one(conn, "SELECT * FROM member WHERE org_id=? AND member_id=?", (org_id, member_id))


def get_org(conn, org_id):
    return one(conn, "SELECT * FROM org WHERE org_id=?", (org_id,))


def get_image(conn, org_id, image_id):
    return one(conn, "SELECT * FROM image WHERE org_id=? AND image_id=?", (org_id, image_id))
