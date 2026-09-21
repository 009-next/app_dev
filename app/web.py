"""Web層（標準ライブラリだけ）。画面は3つ: 物のページ（X10）・カードの作成（X3）・共有リンクの閲覧（H10）。

- 処理の本体は handle()。http.server は薄い皮なので、テストは handle() を直接呼ぶ。
- 権限は毎回サーバー側で判定する（authz.can は objects / cards の中で通る）。ここで画面を隠すだけの制御はしない。
- 存在しない・無効・期限切れ・権限なしの共有／画像は、区別なく同じ 404 を返す（N4）。
- 匿名の閲覧には回数の制限をかけ、閲覧は回数だけを記録する（個人を追跡しない）。
- 写真は、ブラウザ（static/mask.js）が Canvas で作り手の指定した範囲を潰してから送る。サーバーに届くのはマスキング後の JPEG だけで、
  元の写真の入力欄は名前を持たず、フォームにも入らない。作り手が「隠すべき箇所をすべて隠した」と確認しない限り、サーバーは写真を受け付けない。
  サーバーでは images.process が EXIF 除去と再エンコードを行う（二重チェック）。
- 開発モード限定（MIRUCON_ENV=dev）。
"""

from __future__ import annotations

import datetime as dt
import email
import html
import json
import os
import pathlib
import re
import sys
import threading
import urllib.parse
from dataclasses import dataclass, field
from http import cookies, server

from . import auth, authz, cards, db, fusion, images, jadate, jobs, notifications, objects, sharing, signals, talk, talk_audio, talk_loop, theme, vision

MAX_BODY = 64_000
MAX_UPLOAD = 12_000_000  # 写真つきのカード作成だけ。1枚 5MB（images.MAX_BYTES）× 最大 2 枚を見込む
PHOTO_PARTS = {"photo_before": "before", "photo_after": "after"}
STATIC_DIR = pathlib.Path(__file__).with_name("static")
SHARE_LIMIT = (60, 60)  # 同じ IP から 60 秒に 60 回まで
DB_PATH = pathlib.Path(__file__).with_name("data") / "mirucon.db"
CLIENT_FACTORY = None  # テストで偽の LLM クライアントを差し込む。None なら llm.default_factory
JOBS = jobs.Inline()  # AI の処理の実行方法。main() が Threaded に替える（カード作成が、AI の処理を待たずに返る）
_REQUEST_LOCK = threading.Lock()  # リクエストの処理は1つずつ（どれも速い）。遅い AI の処理は、この外の別スレッドで行う

_hits: dict[str, list[float]] = {}


def reset_limits() -> None:
    _hits.clear()


def _limited(key: str, limit: int, window: int) -> bool:
    t = db.now()
    hits = [x for x in _hits.get(key, []) if t - x < window]
    if len(hits) >= limit:
        _hits[key] = hits
        return True
    hits.append(t)
    _hits[key] = hits
    return False


esc = html.escape


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: list = field(default_factory=list)
    content_type: str = "text/html; charset=utf-8"
    permissions: str | None = None  # 既定（マイクなし）を上書きする画面だけ指定する。会話のページ（/o/…/talk）が、マイクを使うときだけ

    def all_headers(self) -> list:
        return [("Content-Type", self.content_type),
                ("Cache-Control", "no-store"),
                # no-referrer だと、ブラウザは同じサイトへのフォーム送信にも Origin: null を付け、_same_origin が全 POST を拒否する。
                # same-origin なら、他のサイトへは Referer（URL 中の共有トークン）を送らず、自分のサイトへの POST には正しい Origin が付く。
                ("Referrer-Policy", "same-origin"),
                ("X-Content-Type-Options", "nosniff"),
                # 使う権限を明示する。画面共有（通話の画面の取り込み）は自分のページだけ。
                # カメラ・マイク・位置情報は使わないので、明示的に閉じる
                ("Permissions-Policy", self.permissions or "display-capture=(self), camera=(), microphone=(), geolocation=()"),
                ("X-Frame-Options", "DENY"),
                ("Content-Security-Policy",
                 "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; "
                 # media-src blob:: 動画から1コマを選ぶとき、端末の中だけで <video> に読み込むため。
                 # 外部の URL は許さない（blob: は、この端末で作った参照だけ）
                 "img-src 'self' blob:; media-src blob:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")] + self.headers


CSS = """
:root{color-scheme:light dark}
body{font:16px/1.6 system-ui,sans-serif;margin:0;padding:16px;max-width:720px;margin-inline:auto;overflow-wrap:anywhere}
h1{font-size:1.4rem;margin:.2em 0 .6em}h2{font-size:1.1rem;margin:1.2em 0 .4em}
a{color:#0b5fff}label{display:block;margin:.8em 0 .2em;font-weight:600}
input,textarea,select,button{font:inherit;padding:.6em;width:100%;box-sizing:border-box;border:1px solid #888;border-radius:6px}
button{background:#0b5fff;color:#fff;border:0;margin-top:.8em;cursor:pointer}
input[type=checkbox]{width:auto;margin-right:.5em}.card{border:1px solid #8888;border-radius:8px;padding:12px;margin:.6em 0}
.muted{color:#777;font-size:.9rem}.ai{border-left:4px solid #0b5fff;padding-left:10px}
img,.mask-canvas,.mask-video{max-width:100%;height:auto;border-radius:6px}.mask-video{display:block;margin:.4em 0}.mask-canvas{touch-action:none;display:block;margin:.4em 0}ul{padding-left:1.2em}
.opt{border-top:1px solid #8884;padding:.6em 0}.opt p{margin:.25em 0}
.k{color:#777;font-size:.85rem;display:inline-block;min-width:11em}
@media (min-width:900px){body{max-width:860px}}
"""


def page(title: str, body: str, status: int = 200, headers: list | None = None, head: str = "") -> Response:
    doc = (f'<!doctype html><html lang="ja"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">{head}<title>{esc(title)}</title>'
           f"<style>{CSS}{theme.THEME}</style></head><body>{theme.FX}<h1>{esc(title)}</h1>{body}</body></html>")
    return Response(status, doc.encode(), headers or [])


def not_found() -> Response:
    return page("見つかりません", "<p>このページは存在しないか、期限切れです。</p>", 404)


SOURCE_NOTE = {
    "video_frame": '<p class="muted">この写真は、動画を一時停止した1コマから取り込みました。</p>',
    "call_screen": '<p class="muted"><b>この写真は、ビデオ通話の画面から取り込みました。</b>'
                   'はじめは全面を隠した状態から、残す所だけを開けています。'
                   '第三者が写りうるため、このカードの共有範囲は<b>最も狭い「招待した人のみ」</b>に固定されています。</p>',
}


def _source_note(source: str) -> str:
    return SOURCE_NOTE.get(source or "camera", "")


def _fmt_time(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


@dataclass
class Req:
    conn: object
    method: str
    path: str
    query: dict
    form: dict
    files: list
    headers: dict
    cookies: dict
    ip: str
    session: object = None
    actor: authz.Actor | None = None

    @property
    def origin(self) -> str:
        return "http://" + self.headers.get("host", "localhost")


def _load_actor(req: Req) -> None:
    row = auth.get_session(req.conn, req.cookies.get("sid"))
    if row is None or row["kind"] != "member":
        return
    m = db.one(req.conn, "SELECT * FROM member WHERE member_id=?", (row["member_id"],))
    req.session = row
    req.actor = authz.Actor(kind="member", org_id=m["org_id"], member_id=m["member_id"], role=m["role"])


def _need_login(req: Req):
    return None if req.actor else Response(303, headers=[("Location", "/login")])


# ---- ログイン ----------------------------------------------------------------

def login_form(req: Req) -> Response:
    return page("ログイン", '<form method="post" action="/login"><label>メールアドレス</label>'
                            '<input name="email" type="email" autocomplete="username" required>'
                            "<button>コードを送る</button></form>")


def login_post(req: Req) -> Response:
    auth.request_login_code(req.conn, req.form.get("email", ""), req.ip)  # 存在しないメールでも同じ画面
    req.conn.commit()
    return page("コードを入力", '<p>入力されたメールアドレスが登録されていれば、コードを送りました。</p>'
                                f'<form method="post" action="/login/verify"><input type="hidden" name="email" '
                                f'value="{esc(req.form.get("email", ""))}"><label>6桁のコード</label>'
                                '<input name="code" inputmode="numeric" autocomplete="one-time-code" required>'
                                "<button>ログイン</button></form>")


def login_verify(req: Req) -> Response:
    email = auth.normalize_email(req.form.get("email", ""))
    m = db.one(req.conn, "SELECT * FROM member WHERE email=? AND status='active'", (email,))
    ok = bool(m) and auth.verify_code(req.conn, "login", m["member_id"], req.form.get("code", "").strip())
    req.conn.commit()
    if not ok:
        return page("ログインできません", '<p>コードが違うか、期限切れです。<a href="/login">やり直す</a></p>', 401)
    token = auth.create_session(req.conn, "member", member_id=m["member_id"])
    req.conn.commit()
    return Response(303, headers=[("Location", "/"), ("Set-Cookie",
                    f"sid={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={auth.SESSION_TTL}")])


def logout(req: Req) -> Response:
    auth.logout(req.conn, req.cookies.get("sid"))
    req.conn.commit()
    return Response(303, headers=[("Location", "/login"), ("Set-Cookie", "sid=; Max-Age=0; Path=/")])


# ---- X10: 物のページ ---------------------------------------------------------

def _external_block(req: Req) -> str:
    """外部モデル（Claude 以外の第三者の提供元）の設定。オーナーだけが見て、切り替えられる。"""
    if not authz.can(req.actor, "manage_members", {"org_id": req.actor.org_id}):
        return ""
    row = db.one(req.conn, "SELECT external_llm FROM org_setting WHERE org_id=?", (req.actor.org_id,))
    on = row is None or bool(row["external_llm"])
    return ('<h2>外部モデルの利用（オーナーの設定）</h2>'
            '<div class="card"><p>カード種類の選択（分類）だけ、条件を満たすときに、Claude 以外の小型モデル（Orca 経由・DeepSeek など）を先に試します。'
            '渡すのは、<b>機微な語・通話の画面・招待限定・600文字超のいずれにも当たらない、短い文章だけ</b>で、'
            '<b>写真は渡しません</b>。文章の一部が、その<b>第三者の提供元</b>へ渡ります。判断の記録に、使ったモデルが残ります。'
            '語句による判定なので、個人情報の完全な検出ではありません。</p>'
            f'<p>いまの設定: <b>{"オン" if on else "オフ"}</b></p>'
            f'<form method="post" action="/settings/external-llm"><input type="hidden" name="enabled" value="{0 if on else 1}">'
            f'<button>{"オフにする（Claude だけを使う）" if on else "オンにする"}</button></form></div>')


def settings_external_llm(req: Req) -> Response:
    if (r := _need_login(req)):
        return r
    if not authz.can(req.actor, "manage_members", {"org_id": req.actor.org_id}):
        return not_found()
    value = 1 if req.form.get("enabled") == "1" else 0
    db.run(req.conn, "INSERT INTO org_setting(org_id, external_llm) VALUES(?,?) "
                     "ON CONFLICT(org_id) DO UPDATE SET external_llm=excluded.external_llm", (req.actor.org_id, value))
    db.audit(req.conn, req.actor.org_id, "org.external_llm", req.actor.member_id, req.actor.org_id, "オン" if value else "オフ")
    req.conn.commit()
    return Response(303, headers=[("Location", "/")])


def _vision_setting_block(req: Req) -> str:
    """通話の画面（フィルター後）を AI に見せる機能の設定。オーナーだけ。既定はオフ。"""
    if not authz.can(req.actor, "manage_members", {"org_id": req.actor.org_id}):
        return ""
    on = vision.enabled(req.conn, req.actor.org_id)
    blocked = vision.industry_blocked(req.conn, req.actor.org_id)
    return ('<h2>画像を AI に見せる機能（オーナーの設定・既定はオフ）</h2>'
            '<div class="card"><p>通話の画面から取り込んだ写真だけが対象です。カードの作り手が、カードごとに確認して押したときだけ、'
            '<b>保存済みの「フィルター後の画像」</b>（作り手が開けた所だけが読めます）を、AI（Claude。Orca 経由の場合は Orca も）へ送ります。'
            'マスキング前の画像は、サーバーにも AI にも届きません。カードの文章に個人情報・医療などの語がある場合や、開いている範囲が広い場合は、送りません。'
            'AI の答えは提案で、共有範囲やぼかしには自動で反映されません。<b>ぼかし損ねた顔や文字は、そのまま提供元へ渡ります。</b></p>'
            f'<p>いまの設定: <b>{"オン" if on else "オフ"}</b></p>'
            f'<form method="post" action="/settings/vision-ai"><input type="hidden" name="enabled" value="{0 if on else 1}">'
            f'<button>{"オフにする" if on else "オンにする"}</button></form>'
            '<p style="margin-top:1em"><b>医療・介護など、機微な現場の組織</b>は、下でオンにすると、通話画面を AI に見せる機能が<b>止まります</b>'
            '（AI の「機微な場面」の判断は、画像の品質で外れることがあるため、組織の設定で決めます）。</p>'
            f'<p>機微な現場の設定: <b>{"オン（画像を AI に見せない）" if blocked else "オフ"}</b></p>'
            f'<form method="post" action="/settings/sensitive-industry"><input type="hidden" name="enabled" value="{0 if blocked else 1}">'
            f'<button>{"オフにする" if blocked else "機微な現場として、画像分析を止める"}</button></form></div>')


def settings_sensitive_industry(req: Req) -> Response:
    if (r := _need_login(req)):
        return r
    if not authz.can(req.actor, "manage_members", {"org_id": req.actor.org_id}):
        return not_found()
    value = 1 if req.form.get("enabled") == "1" else 0
    _set_org_flag(req, "sensitive_industry", value, "org.sensitive_industry")
    return Response(303, headers=[("Location", "/")])


def settings_vision_ai(req: Req) -> Response:
    if (r := _need_login(req)):
        return r
    if not authz.can(req.actor, "manage_members", {"org_id": req.actor.org_id}):
        return not_found()
    value = 1 if req.form.get("enabled") == "1" else 0
    db.run(req.conn, "INSERT INTO org_setting(org_id, external_llm, vision_llm) VALUES(?,1,?) "
                     "ON CONFLICT(org_id) DO UPDATE SET vision_llm=excluded.vision_llm", (req.actor.org_id, value))
    db.audit(req.conn, req.actor.org_id, "org.vision_llm", req.actor.member_id, req.actor.org_id, "オン" if value else "オフ")
    req.conn.commit()
    return Response(303, headers=[("Location", "/")])


def home(req: Req) -> Response:
    if (r := _need_login(req)):
        return r
    rows = db.many(req.conn, "SELECT obj_id,name FROM object WHERE org_id=? ORDER BY created_at DESC LIMIT 100",
                   (req.actor.org_id,))
    items = "".join(f'<li><a href="/o/{esc(r["obj_id"])}">{esc(r["name"])}</a></li>' for r in rows) or "<li>まだありません</li>"
    return page("物の一覧", f"<ul>{items}</ul>"
                            '<p><a href="/n">通知の下書き（承認待ち）</a></p><h2>物を登録する</h2>'
                            '<form method="post" action="/objects"><label>名前</label><input name="name" required maxlength="100">'
                            '<label>次回点検日（任意。過ぎるとAIが通知案を作ります）</label><input name="next_check" type="date">'
                            "<button>登録してタグを発行</button></form>"
                            f'{_external_block(req)}{_vision_setting_block(req)}{_talk_setting_block(req)}{_fusion_setting_block(req)}{_gmail_pref_block(req)}'
                            '<form method="post" action="/logout"><button>ログアウト</button></form>')


def object_create(req: Req) -> Response:
    if (r := _need_login(req)):
        return r
    obj, _tag = objects.register_object(req.conn, req.actor, req.form.get("name", ""),
                                        next_check=req.form.get("next_check", "").strip() or None)
    return Response(303, headers=[("Location", f"/o/{obj['obj_id']}")])


def tag_page(req: Req, tag_id: str) -> Response:
    if _limited("t:" + req.ip, *SHARE_LIMIT):
        return page("しばらくしてからお試しください", "", 429)
    info = objects.resolve_tag(req.conn, tag_id, req.actor)
    if info is None:
        return not_found()
    body = f'<p class="muted">問い合わせ先: {esc(info["contact"])}</p>'
    if "obj_id" in info:
        body += f'<p><a href="/o/{esc(info["obj_id"])}">記録を見る・カードを作る</a></p>'
    return page(info["name"], body)


LEVEL_LABEL = {"high": "高", "warning": "要確認"}


def _notes_block(notes: list[dict], visible: set) -> str:
    """確認事項（コードだけの検知）。各件に、検出した規則の名前と、元の記録へのリンクを付ける。"""
    if not [n for n in notes if n["rule"] != "call_screen_card" or any(i in visible for i in n["evidence_ids"])]:
        return '<div class="card"><b>確認事項</b><p class="muted">コードの規則では、確認すべき点は見つかりませんでした。</p></div>'
    items = ""
    for n in notes:
        if n["rule"] == "call_screen_card" and not any(i in visible for i in n["evidence_ids"]):
            continue  # 招待限定のカードの存在を、見られない人に漏らさない
        links = "".join(f' <a href="/c/{esc(i)}">記録</a>' for i in n["evidence_ids"] if i in visible)
        items += (f'<li>[{esc(LEVEL_LABEL.get(n["level"], n["level"]))}] {esc(n["message"])}'
                  f'<br><span class="muted">検出規則: {esc(n["rule"])}{links}</span></li>')
    return ('<div class="card"><b>確認事項</b>'
            '<p class="muted">コードの規則で見つけたものです。AI の判断ではなく、異常が確定したものでもありません。'
            '語句による限定的な検知で、見落としがあります。</p>'
            f'<ul>{items}</ul></div>')


def _timeline_block(rows: list[dict]) -> str:
    if not rows:
        return ""
    items = ""
    for r in rows:
        when = _fmt_time(r["at"])
        if r["kind"] == "gap":
            items += f'<li class="muted">{esc(when)} — {esc(r["label"])}</li>'
        elif r["kind"] == "card":
            items += (f'<li>{esc(when)}・カード <a href="/c/{esc(r["id"])}">{esc(r["label"])}</a> '
                      f'<span class="muted">{esc(r["card_type"])}</span></li>')
        else:
            items += (f'<li class="muted">{esc(when)}・AIの判断（{esc(r["stage"])}）: {esc(r["label"])}</li>')
    return f'<h2>時系列</h2><ul>{items}</ul>'


def _decisions_block(rows: list[dict], spend: float) -> str:
    """AI の判断の記録。何を選び、なぜ選び、どの資料を根拠にし、何を渡さなかったかまで出す。"""
    decs = [r["row"] for r in rows if r["kind"] == "decision"]
    if not decs:
        return ""
    items = ""
    for d in decs:
        src = ""
        if d["evidence_source"]:
            try:
                e = json.loads(d["evidence_source"])
                link = f' <a href="/c/{esc(e["card_id"])}">該当のカード</a>' if e.get("card_id") else ""
                src = f'<br>根拠の出どころ: {esc(e.get("field", ""))}{link}'
            except (json.JSONDecodeError, KeyError):
                src = ""
        dropped = ""
        if d["room_reasons"]:
            try:
                dropped = "<br>渡さなかったツール: " + esc("／".join(json.loads(d["room_reasons"])))
            except json.JSONDecodeError:
                dropped = ""
        third = ""
        if "第三者の提供元のモデルを使った" in (d["validation"] or ""):
            who = (d["validation"].split("第三者の提供元のモデルを使った:")[1].split("（")[0]).strip()
            third = (f'<br><b>第三者の提供元（{esc(who)}）のモデルで判断しました。</b>'
                     'カードの文章の一部が、その提供元へ渡っています。')
        items += (f'<li><b>{esc(d["stage"])}</b> → {esc(d["chosen_tool"] or "")}{third}'
                  f'<br>理由: {esc(d["reason"] or "（記録なし）")}'
                  f'<br>根拠: {esc(d["evidence"] or "（引用なし）")}{src}'
                  f'<br><span class="muted">危険度: {esc(d["risk"] or "low")}'
                  f'・モデル: {esc((d["model"] or "—").split("/")[-1])}'
                  f'・原価: ${float(d["cost_usd"] or 0):.4f}'
                  f'・{esc(d["validation"] or "")}{dropped}</span></li>')
    return (f'<details><summary>AI の判断の記録（{len(decs)}件）</summary>'
            f'<p class="muted">この物で使った AI の原価の合計: ${spend:.4f}</p>'
            f'<ul>{items}</ul></details>')


def _options_block(req: Req, obj_id: str) -> str:
    """次の行動の案（D7・フォース・ビジョン）。条件付きの比較であって、断定でも作業の指示でもない。"""
    data = objects.stored_options(req.conn, req.actor.org_id, obj_id)
    refused = objects.last_options_refusal(req.conn, req.actor.org_id, obj_id)
    note = (f'<p class="muted">前回は、案を作りませんでした（{esc(refused)}）。'
            '入力にない数値や型番を含む案は、コードが受け付けません。もう一度押すと、作り直します。</p>') if refused else ""
    button = (f'<form method="post" action="/o/{esc(obj_id)}/options">'
              f'<button>次の行動の案を{"作り直す" if data else "作る"}</button></form>')
    if not data:
        return ('<div class="card"><b>次の行動の案</b>'
                '<p class="muted">押したときだけ、AI が記録をもとに選択肢を比べます（1回の呼び出しで、少額の費用がかかります）。</p>'
                f'{note}{button}</div>')
    # 6項目の表は、スマホ幅で読めないので、案ごとのまとまりにして縦に並べる
    rows = "".join(
        f'<div class="opt"><b>{esc(o["title"])}</b>'
        f'<p><span class="k">どんな条件のとき</span> {esc(o["condition"])}</p>'
        f'<p><span class="k">期待できること（推測）</span> {esc(o["expect"])}</p>'
        f'<p><span class="k">リスク・分からないこと</span> {esc(o["risk"])}</p>'
        f'<p class="muted"><span class="k">必要時間</span> {esc(o["duration"])}'
        f'　<span class="k">費用</span> {esc(o["cost"])}</p></div>'
        for o in data["options"])
    src = "".join(f' <a href="/c/{esc(i)}">根拠</a>' for i in data.get("evidence_card_ids", [])
                  if db.get_card(req.conn, req.actor.org_id, i))
    conf = data.get("confidence") or {}
    stale = ('<p class="muted"><b>新しい記録があります。</b>この案は、そのあとの記録を見ていません。作り直してください。</p>'
             if data.get("stale") else "")
    return ('<div class="card"><b>次の行動の案</b>'
            '<p class="muted">これは<b>条件付きの案</b>です。どれか1つに<b>断定</b>したものではなく、'
            '作業の<b>指示ではありません</b>。実施するかどうかは人が決めます。'
            '必要時間・費用は、記録に数値がなければ「未計測」と書きます。</p>'
            f'{stale}'
            f'{rows}'
            f'{note}<p class="muted">根拠にした記録: {conf.get("dated", 0)}件（最新 {esc(str(conf.get("latest_date") or "—"))}）。'
            f'{esc(conf.get("explanation", ""))}{src}</p>{button}</div>')


def object_options(req: Req, obj_id: str) -> Response:
    """押したときだけ、次の行動の案を作る（カード作成の流れには入れない）。"""
    if (r := _need_login(req)):
        return r
    objects.next_actions(req.conn, req.actor, obj_id, client_factory=CLIENT_FACTORY)
    return Response(303, headers=[("Location", f"/o/{obj_id}")])


def object_page(req: Req, obj_id: str) -> Response:
    """物の作業室。今の状態・確認事項・時系列・AIの判断・承認待ち・タグを1画面にまとめる。"""
    if (r := _need_login(req)):
        return r
    obj = objects.get_object_for(req.conn, req.actor, obj_id)
    history = objects.object_history(req.conn, req.actor, obj_id)
    visible = {c["card_id"] for c in history}
    items = "".join(
        f'<li><a href="/c/{esc(c["card_id"])}">{esc(c["title"] or "（無題）")}</a> '
        f'<span class="muted">{esc(_fmt_time(c["created_at"]))}・{esc(c["card_type"] or "")}</span></li>'
        for c in reversed(history)) or "<li>まだカードがありません</li>"

    summary = ""
    if obj["summary"] and obj["summary_status"] in ("current", "held"):
        held = "<br><span class=\"muted\">更新を保留中です（新しいカードと矛盾している可能性、または根拠が足りません）。</span>"             if obj["summary_status"] == "held" else ""
        src = "".join(f' <a href="/c/{esc(i)}">根拠</a>' for i in json.loads(obj["summary_sources"] or "[]")
                      if db.get_card(req.conn, req.actor.org_id, i))
        newest = max((c["created_at"] for c in history), default=None)
        fresh = ""
        if newest is not None and (db.now() - newest) >= signals.STALE_DAYS * 86400.0:
            fresh = ('<br><span class="muted">最新の記録から'
                     f'{int((db.now() - newest) / 86400.0)}日たっています。今の状態は変わっている可能性があります。</span>')
        when = f'<br><span class="muted">最終更新: {esc(_fmt_time(newest))}</span>' if newest else ""
        summary = (f'<div class="card ai"><b>経緯と今の状態</b>（AIが書いた文章）<br>{esc(obj["summary"])}{held}{fresh}{when}'
                   f'<br><span class="muted">根拠のカード:{src}</span></div>')

    notes = signals.notes(req.conn, req.actor.org_id, obj_id)
    rows = objects.timeline(req.conn, req.actor, obj_id)
    spend = objects.object_spend(req.conn, req.actor.org_id, obj_id)
    pending = [n for n in notifications.list_drafts(req.conn, req.actor) if n["obj_id"] == obj_id]
    approvals = (f'<div class="card"><b>承認待ち</b><p>この物の通知の下書きが {len(pending)} 件あります。'
                 f'<a href="/n">確認する</a></p></div>') if pending else ""

    tag_items = "".join(
        f'<li class="muted">{esc(req.origin)}/t/{esc(t["tag_id"])}'
        + (f'<br>最後に読まれた: {esc(_fmt_time(t["last_read_at"]))}' if t["last_read_at"] else "<br>最後に読まれた: まだありません")
        + "</li>" for t in objects.tags_of(req.conn, req.actor.org_id, obj_id))

    return page(obj["name"],
                f'{summary}{_notes_block(notes, visible)}{approvals}{_options_block(req, obj_id)}'
                f'<h2>カード</h2><ul>{items}</ul>'
                f'<p><a href="/o/{esc(obj_id)}/new">カードを作る</a></p>'
                f'{_talk_link(req, obj_id)}'
                f'{_timeline_block(rows)}'
                f'{_decisions_block(rows, spend)}'
                f'<h2>タグ（QR・NFC に書く中身）</h2><ul>{tag_items}</ul>'
                '<p class="muted">この画面は、作り手が自分の担当する物を確認するためのものです。'
                '人の居場所や行動を監視するためのものではありません。</p>'
                '<p><a href="/">一覧へ</a></p>')


# ---- X3: カードの作成・管理 ---------------------------------------------------

def card_new(req: Req, obj_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    obj = objects.get_object_for(req.conn, req.actor, obj_id)
    pre = {}
    if req.query.get("talk"):  # 会話から次を考える機能の提案（作り手が同意したもの）の事前入力。作成（送信）は、作り手が行う
        try:
            pre = talk.prefill_for(req.conn, req.actor, req.query["talk"], int(req.query.get("step", "0"))) or {}
        except ValueError:
            pre = {}
    opts = "".join(f'<option value="{s}"{" selected" if s == cards.DEFAULT_SCOPE else ""}>{esc(SCOPE_LABEL[s])}</option>'
                   for s in authz.SCOPES)
    widgets = "".join(
        f'<div class="mask-widget" data-role="{role}"><label>{label}の写真（任意）</label>'
        # 動画も選べる。ただし送るのは、作り手が選んだ1コマを写真にしたものだけ（動画そのものは端末から出ない）
        '<input type="file" accept="image/*,video/*" class="mask-file">'  # name を付けない: フォームには入らない
        '<video class="mask-video" controls playsinline muted hidden></video>'
        '<button type="button" class="mask-share" hidden>通話の画面から取り込む</button>'
        '<button type="button" class="mask-pick" hidden>この画面を写真にする</button>'
        '<canvas class="mask-canvas" hidden></canvas>'
        '<p class="muted mask-area" role="status" hidden></p>'
        '<p class="muted mask-help" hidden>顔・ナンバー・書類など、写ってはいけない所を指でなぞって隠してください。</p>'
        f'<input type="hidden" class="mask-source" name="photo_source_{role}" value="camera">'
        '<button type="button" class="mask-undo" hidden>1つ戻す</button></div>'
        for role, label in (("before", "作業前"), ("after", "作業後")))
    return page(f"カードを作る: {obj['name']}",
                f'<form id="card-form" method="post" action="/o/{esc(obj_id)}/cards" data-mask-form>'
                '<label>作業前の説明</label><textarea name="before_desc" rows="3" maxlength="2000">' + esc(pre.get("before_desc", "")) + '</textarea>'
                '<label>作業後の説明</label><textarea name="after_desc" rows="3" maxlength="2000"></textarea>'
                '<label>吹き込み（文字）</label><textarea name="voice_text" rows="3" maxlength="2000">' + esc(pre.get("voice_text", "")) + '</textarea>'
                # 通話の画面を取り込むときの、開け方の切り替え。既定は旧来（四角だけ・従来の面積の門）。選ぶと、なぞって囲めて、開けた面積を端末が測る
                '<div class="card"><label for="mask-mode">通話の画面の開け方</label>'
                '<select id="mask-mode" class="mask-mode-select"><option value="legacy">旧来（四角のみ）</option>'
                '<option value="extended">拡張（四角＋なぞって囲む。開けた面積を測って表示）</option></select>'
                '<div class="mask-tool-wrap" hidden><label for="mask-tool">開ける道具</label>'
                '<select id="mask-tool" class="mask-tool-select"><option value="rect">四角（ドラッグ）</option><option value="path">なぞって囲む（指・マウス）</option></select></div>'
                '<label><input type="checkbox" class="mask-rec-as-call">動画ファイルを、通話の録画として取り込む（デモ・検証用。全部隠して、見せる所だけを開けます）</label>'
                '<p class="muted">この設定は、通話の画面から取り込んだ写真だけに効きます。</p></div>'
                f"{widgets}"
                '<label class="mask-confirm" hidden><input type="checkbox" id="mask-confirmed">'
                "隠すべき箇所をすべて隠しました</label>"
                f'<label>誰が見られるか</label><select name="scope">{opts}</select>'
                '<p class="muted">写真はこの端末で隠してから送ります。元の写真は送りません。</p>'
                '<p id="mask-error" class="muted" role="alert"></p>'
                '<button>作成（AIが説明文を書きます）</button></form>'
                '<script src="/static/mask.js" defer></script>')


SCOPE_LABEL = {"link_30d": "リンクを知っている人（30日）", "org_only": "組織内のみ", "invited_only": "招待した人のみ"}


def _float_or_none(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def card_create(req: Req, obj_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    f = req.form
    photos = []
    for name, data in req.files:
        role = PHOTO_PARTS.get(name)
        if role is None or not data:
            raise ValueError("写真の形式が正しくありません")
        # ぼかしは端末側で済んでいる。ここでは EXIF 除去と再エンコードだけ。4つ目は出どころ
        photos.append((role, data, None, f.get(f"photo_source_{role}", "camera"), _float_or_none(f.get(f"open_ratio_{role}"))))
    confirmed = f.get("mask_confirmed") == "1"
    if photos and not confirmed:
        raise ValueError("写真は、隠すべき箇所の確認をしてから送ってください")
    kw = dict(before_desc=f.get("before_desc", "")[:2000], after_desc=f.get("after_desc", "")[:2000],
              voice_text=f.get("voice_text", "")[:2000], scope=f.get("scope", cards.DEFAULT_SCOPE), images_in=photos,
              mask_confirmed=confirmed, client_factory=CLIENT_FACTORY)
    if JOBS.is_async:  # カードを保存したらすぐ返す。AI の結果は、あとから付く（画面が自動で更新される）
        out = cards.create_card_async(req.conn, req.actor, obj_id, jobs=JOBS, **kw)
    else:
        out = cards.create_card(req.conn, req.actor, obj_id, **kw)
    return Response(303, headers=[("Location", f"/c/{out['card']['card_id']}")])


def _ai_block(card) -> str:
    if not card["ai_text"]:
        return ""
    t = json.loads(card["ai_text"])
    changes = "".join(f"<li>{esc(c)}</li>" for c in t.get("changes", []))
    return (f'<div class="card ai"><b>{esc(t.get("title", ""))}</b> <span class="muted">（AIが書いた文章）</span>'
            f"<ul>{changes}</ul><p>{esc(t.get('description', ''))}</p></div>")


KIND_LABEL = {"face": "顔", "name": "名前", "text": "文字", "sign": "看板", "document": "書類", "screen": "画面", "other": "その他"}


def _vision_block(req: Req, card, imgs) -> tuple[str, bool]:
    """通話の画面の写真ごとに、「AI に見せる」の操作と、結果（提案）を出す。作り手（編集できる人）だけ。"""
    if not vision.may_start(req.actor, card):
        return "", False
    out, running = "", False
    on = vision.enabled(req.conn, req.actor.org_id)
    blocked = vision.industry_blocked(req.conn, req.actor.org_id)
    for i in imgs:
        if i["source"] != "call_screen":
            continue
        v = vision.latest(req.conn, req.actor.org_id, card["card_id"], i["image_id"])
        body = ""
        if v is not None and v["status"] == "running" and db.now() - v["created_at"] < 120:
            running = True
            body += '<p class="muted">AI が画像を見ています。自動で更新されます…</p>'
        elif v is not None and v["status"] == "done" and v["result"]:
            r = json.loads(v["result"])
            body += (f'<p><b>AI が、フィルター後の画像から見たこと（提案）</b> <span class="muted">（{esc(v["model"] or "")}）</span></p>'
                     f'<p>{esc(r["visible_summary"])}</p>')
            if r["work_inference"]:
                body += "<ul>" + "".join(f'<li>{esc(w["claim"])}<br><span class="muted">根拠: {esc(w["basis"])}／確信度: {esc(w["confidence"])}／未確認</span></li>'
                                        for w in r["work_inference"]) + "</ul>"
            elif r["sensitive_setting"]:
                body += '<p class="muted">機微な場面に見えるため、作業内容の推測は控えました。共有の前に、人が確認してください。</p>'
            if r["residual_identifiers"]:
                body += "<p><b>開いている範囲に、残っているもの</b>（追加でぼかすことをおすすめします）</p><ul>"
                for k, ri in enumerate(r["residual_identifiers"]):
                    x, y, w, h = ri["box"]
                    body += (f'<li>{esc(KIND_LABEL.get(ri["kind"], "その他"))}（{"読める" if ri["legible"] else "読めない"}）: '
                             f'画面の左から{int(x * 100)}%・上から{int(y * 100)}%あたり'
                             f'<form method="post" action="/c/{esc(card["card_id"])}/mask/{esc(i["image_id"])}">'
                             f'<input type="hidden" name="rects" value="{esc(json.dumps([[x, y, w, h]]))}"><button>この範囲を隠す</button></form></li>')
                body += "</ul>"
            elif r["recommend_mask"]:
                body += "<p>追加でぼかすことを、AI がすすめています。</p>"
            body += f'<p class="muted">見えていないもの: {esc(r["not_visible"])}</p>'
        elif v is not None and v["status"] in ("rejected", "failed"):
            body += '<p class="muted">AI の分析は、検査で採用しませんでした（または失敗しました）。カードには影響しません。</p>'
        if on and not blocked:
            try:
                for msg in vision.image_warnings(cards.locate_image(i["path"]).read_bytes()):
                    body += f'<p class="muted">⚠ {esc(msg)}</p>'
            except OSError:
                pass
        if on and blocked:
            body += '<p class="muted">この組織は、医療・介護など機微な現場と設定されているため、画像を AI に見せません。</p>'
        elif on:
            body += (f'<form method="post" action="/c/{esc(card["card_id"])}/vision/{esc(i["image_id"])}">'
                     '<label><input type="checkbox" name="confirmed" value="1" required>この画像（作り手が開けた所だけ）が、AI の提供元へ渡ることを確認しました。'
                     '開けた所に顔や文字が残っていれば、それも渡ります</label>'
                     f'<button>この画像を AI に見せて、分析する</button></form>')
        elif v is None:
            body += '<p class="muted">画像を AI に見せる機能は、この組織ではオフです（オーナーが設定します）。</p>'
        out += f'<div class="card ai"><b>通話の画面の写真（{esc(i["role"])}）</b>{body}</div>'
    return out, running


def card_vision_start(req: Req, card_id: str, image_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    try:
        vision.start(req.conn, req.actor, card_id, image_id, confirmed=req.form.get("confirmed") == "1",
                     jobs=JOBS, client_factory=CLIENT_FACTORY)
    except vision.VisionRefused as e:
        return page("AI に見せられません", f"<p>{esc(str(e))}</p><p><a href=\"/c/{esc(card_id)}\">カードへ戻る</a></p>", 400)
    return Response(303, headers=[("Location", f"/c/{card_id}")])


def card_page(req: Req, card_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    c = cards.get_card(req.conn, req.actor, card_id)
    imgs = db.many(req.conn, "SELECT image_id, role, source, path FROM image WHERE card_id=? ORDER BY created_at", (card_id,))
    img_html = "".join(f'<img src="/mimg/{esc(i["image_id"])}" alt="{esc(i["role"])}">'
                       f'{_source_note(i["source"])}'
                       f'<p><a href="/c/{esc(card_id)}/mask/{esc(i["image_id"])}">この写真に隠す範囲を追加する</a></p>' for i in imgs)
    ai_note, head = "", ""
    if c["ai_status"] == "running":
        if db.now() - c["created_at"] < 180:
            ai_note = ('<p class="card ai">説明文ができました。共有の確認と要約を続けています。自動で更新されます…</p>' if c["ai_text"] else
                       '<p class="card ai">AIが、種類・説明文・要約を作成しています。数秒後に自動で更新されます…</p>')
            head = '<meta http-equiv="refresh" content="3">'
        else:
            ai_note = '<p class="card">AIの処理が完了していません（中断された可能性があります）。カードは保存されています。</p>'
    elif c["ai_status"] == "failed":
        ai_note = '<p class="card">AIの処理に失敗しました。カードは保存されています。</p>'
    elif c["ai_status"] == "timeout":
        ai_note = '<p class="card">AIの処理に時間がかかっています。出せた結果だけを表示しています。カードは保存されています。</p>'
    narrower = [s for s in authz.SCOPES if authz.scope_is_narrower(s, c["scope"])]
    prop = ""
    if c["proposed_scope"]:
        prop = (f'<div class="card ai"><b>AIの提案</b>: 共有範囲を「{esc(SCOPE_LABEL[c["proposed_scope"]])}」に狭める '
                f'<form method="post" action="/c/{esc(card_id)}/narrow"><input type="hidden" name="scope" '
                f'value="{esc(c["proposed_scope"])}"><button>この提案を適用する</button></form></div>')
    mask = ""
    if c["proposed_mask"]:
        try:
            pm = json.loads(c["proposed_mask"])
        except ValueError:
            pm = {}
        mask = (f'<div class="card ai"><b>AIの提案（追加のぼかし）</b><br>隠したほうがよい箇所: {esc(str(pm.get("target", "")))}'
                f'<br><span class="muted">{esc(str(pm.get("reason", "")))}</span>'
                + "".join(f'<br><a href="/c/{esc(card_id)}/mask/{esc(i["image_id"])}">{esc(i["role"])}の写真で、隠す範囲を指定する</a>' for i in imgs) +
                '<br><span class="muted">写真で範囲を指定して隠すか、不要と判断したら、確認済みにしてください。共有の前に確認をお願いします。</span>'
                f'<form method="post" action="/c/{esc(card_id)}/mask-ack"><button>確認した</button></form></div>')
    vis, vis_running = _vision_block(req, c, imgs)
    fus, fus_running = _fusion_block(req, c, imgs)
    vis += fus
    vis_running = vis_running or fus_running
    if vis_running and not head:
        head = '<meta http-equiv="refresh" content="3">'
    narrow = "".join(f'<option value="{s}">{esc(SCOPE_LABEL[s])}</option>' for s in narrower)
    narrow_form = (f'<form method="post" action="/c/{esc(card_id)}/narrow"><select name="scope">{narrow}</select>'
                   "<button>共有範囲を狭める</button></form>") if narrower else ""
    share = ""
    if c["scope"] == "link_30d":
        share = (f'<form method="post" action="/c/{esc(card_id)}/share"><label><input type="checkbox" name="confirmed" '
                 'value="1">内容とぼかしを確認しました</label><button>共有リンクを発行</button></form>')
        if c["proposed_mask"]:
            share = '<p class="card">AIの追加のぼかしの提案が、まだ確認されていません。上の提案を確認してから共有してください。</p>' + share
    pend = f'<p class="card">AIからの質問: {esc(c["pending_question"])}</p>' if c["pending_question"] else ""
    return page(c["title"] or "カード", head=head, body=
                f'<p class="muted">共有範囲: {esc(SCOPE_LABEL[c["scope"]])}・種類: {esc(c["card_type"] or "")}</p>{ai_note}{pend}'
                f'{_ai_block(c)}<h2>作業前</h2><p>{esc(c["before_desc"] or "")}</p><h2>作業後</h2><p>{esc(c["after_desc"] or "")}</p>'
                f'{img_html}{vis}{prop}{mask}<h2>共有</h2>{narrow_form}{share}<p><a href="/o/{esc(c["obj_id"])}">物のページへ</a></p>')


def card_mask_ack(req: Req, card_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    try:
        cards.dismiss_mask_proposal(req.conn, req.actor, card_id)
    except ValueError as e:
        return page("確認できません", f"<p>{esc(str(e))}</p>", 400)
    return Response(303, headers=[("Location", f"/c/{card_id}")])


def mask_edit_page(req: Req, card_id: str, image_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    cards.get_card(req.conn, req.actor, card_id)
    img = db.get_image(req.conn, req.actor.org_id, image_id)
    if img is None or img["card_id"] != card_id:
        return not_found()
    return page("隠す範囲を指定する",
                '<p class="muted">隠したい所を、指でなぞって囲んでください（何か所でも）。保存すると、その範囲がモザイクになり、元には戻せません。</p>'
                f'<canvas id="mask-edit-canvas" data-src="/mimg/{esc(image_id)}" hidden></canvas>'
                f'<form id="mask-edit-form" method="post" action="/c/{esc(card_id)}/mask/{esc(image_id)}">'
                '<input type="hidden" name="rects" value="[]"><p id="mask-edit-count" class="muted" role="status"></p>'
                '<button type="button" id="mask-edit-undo" hidden>1つ戻す</button>'
                '<button id="mask-edit-save" disabled>この範囲を隠して保存</button></form>'
                f'<p><a href="/c/{esc(card_id)}">カードへ戻る</a></p>'
                '<script src="/static/mask-edit.js" defer></script>')


def mask_edit_save(req: Req, card_id: str, image_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    try:
        rects = json.loads(req.form.get("rects", "[]"))
    except ValueError:
        raise ValueError("範囲の形式が正しくありません") from None
    cards.add_mask_rects(req.conn, req.actor, card_id, image_id, rects)
    return Response(303, headers=[("Location", f"/c/{card_id}")])


def card_narrow(req: Req, card_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    try:
        cards.narrow_scope(req.conn, req.actor, card_id, req.form.get("scope", ""))
    except ValueError as e:
        return page("変更できません", f"<p>{esc(str(e))}</p>", 400)
    return Response(303, headers=[("Location", f"/c/{card_id}")])


def card_share(req: Req, card_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    if not auth.recent_auth(req.session):  # N5: 再認証
        return page("再認証が必要です", '<p>共有リンクの発行には、直近のログインが必要です。<a href="/login">ログインし直す</a></p>', 403)
    try:
        s = cards.issue_share(req.conn, req.actor, card_id, confirmed=req.form.get("confirmed") == "1")
    except cards.NotReady as e:
        return page("発行できません", f"<p>{esc(str(e))}</p>", 400)
    url = f"{req.origin}/s/{s['token']}"
    return page("共有リンクを発行しました", f'<p class="card">{esc(url)}</p><p class="muted">期限: '
                                          f'{esc(_fmt_time(s["expires_at"]))}。取り消すまで有効です。</p>'
                                          f'<p><a href="/c/{esc(card_id)}">カードへ</a></p>')


# ---- H10: 受け手の閲覧（ログイン不要） ---------------------------------------

def share_view(req: Req, token: str) -> Response:
    if _limited("s:" + req.ip, *SHARE_LIMIT):
        return page("しばらくしてからお試しください", "", 429)
    hit = sharing.resolve_share(req.conn, token)
    if hit is None:
        return not_found()
    share, card = hit
    db.run(req.conn, "UPDATE share SET view_count=view_count+1 WHERE share_id=?", (share["share_id"],))
    req.conn.commit()
    org = db.get_org(req.conn, card["org_id"])
    imgs = db.many(req.conn, "SELECT image_id, role FROM image WHERE card_id=? ORDER BY created_at", (card["card_id"],))
    img_html = "".join(f'<img src="{esc(sharing.sign_image(req.conn, i["image_id"], "share:" + share["share_id"]))}" '
                       f'alt="{esc(i["role"])}">' for i in imgs)
    body = (f'<p class="muted">作成: {esc(org["name"])}・{esc(_fmt_time(card["created_at"]))}</p>{_ai_block(card)}'
            f'<h2>作業前</h2><p>{esc(card["before_desc"] or "")}</p><h2>作業後</h2><p>{esc(card["after_desc"] or "")}</p>{img_html}'
            f'<p class="muted">問い合わせ先: {esc(org["contact"])}</p>')
    return page(card["title"] or "作業報告", body)


# ---- 通知の下書き（AI が作った案を、人が承認・却下する。承認しても送信はしない） -----------

def notifications_page(req: Req) -> Response:
    if (r := _need_login(req)):
        return r
    rows = notifications.list_drafts(req.conn, req.actor)
    # 承認の前に、対象・本文・影響範囲を出す。承認は、ここに出した本文そのものに紐づける（digest）
    items = "".join(
        f'<div class="card"><b>{esc(x["obj_name"])}</b> <span class="muted">宛先: {esc(x["recipient"])}・AIが作った案</span>'
        f'<p>{esc(x["message"])}</p>'
        f'<p class="muted">承認すると: この案を「承認済み」として記録します。<b>送信はしません</b>。'
        f'変えるのはこの下書きの状態だけで、カードや共有範囲には影響しません。</p>'
        f'<form method="post" action="/n/{esc(x["notif_id"])}/approve">'
        f'<input type="hidden" name="digest" value="{esc(notifications.draft_digest(req.conn, x))}">'
        f'<button>承認する</button></form>'
        f'<form method="post" action="/n/{esc(x["notif_id"])}/reject"><button type="submit">却下する</button></form></div>'
        for x in rows) or "<p>承認待ちの下書きはありません。</p>"
    return page("通知の下書き", f'{items}<p class="muted">承認しても、メールはまだ送信されません（配信は未対応）。</p>'
                                '<p><a href="/">一覧へ</a></p>')


def notification_approve(req: Req, notif_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    if not auth.recent_auth(req.session):  # N5: 再認証
        return page("再認証が必要です", '<p>承認には、直近のログインが必要です。<a href="/login">ログインし直す</a></p>', 403)
    try:
        notifications.decide_draft(req.conn, req.actor, notif_id, True, digest=req.form.get("digest"))
    except notifications.StaleDraft:
        return page("画面が古くなっています", '<p>この下書きの内容が、画面に出したものと違います。'
                                              '<a href="/n">読み直して</a>から、もう一度確認してください。</p>', 409)
    return Response(303, headers=[("Location", "/n")])


def notification_reject(req: Req, notif_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    notifications.decide_draft(req.conn, req.actor, notif_id, False)
    return Response(303, headers=[("Location", "/n")])


def _serve_file(path: str) -> Response:
    p = pathlib.Path(path)
    if not p.is_file():
        return not_found()
    return Response(200, p.read_bytes(), content_type="image/jpeg")


def image_shared(req: Req, image_id: str) -> Response:
    if _limited("i:" + req.ip, 300, 60):
        return page("しばらくしてからお試しください", "", 429)
    img = db.one(req.conn, "SELECT * FROM image WHERE image_id=?", (image_id,))
    q = req.query
    if img is None or not sharing.verify_image_request(req.conn, img, q.get("u", ""), q.get("e", ""), q.get("g", "")):
        return not_found()
    return _serve_file(str(cards.locate_image(img["path"])))


def image_member(req: Req, image_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    img = db.get_image(req.conn, req.actor.org_id, image_id)
    if img is None:
        return not_found()
    try:
        cards.get_card(req.conn, req.actor, img["card_id"])
    except objects.NotFound:
        return not_found()
    return _serve_file(str(cards.locate_image(img["path"])))


def static_file(req: Req, name: str) -> Response:
    if name not in ("mask.js", "mask-edit.js", "talk.js"):  # 配るファイルは固定。任意のパスは読ませない
        return not_found()
    return Response(200, (STATIC_DIR / name).read_bytes(), content_type="application/javascript; charset=utf-8")



# ---- 会話から次を考える（既定オフ。app/talk.py・talk_loop.py・talk_audio.py）----------------------------------

def _talk_link(req: Req, obj_id: str) -> str:
    if not talk.enabled(req.conn, req.actor.org_id):
        return ""
    return f'<p><a href="/o/{esc(obj_id)}/talk">会話から次の業務を考える</a></p>'


def _talk_setting_block(req: Req) -> str:
    if not authz.can(req.actor, "manage_members", {"org_id": req.actor.org_id}):
        return ""
    on = talk.enabled(req.conn, req.actor.org_id)
    audio = talk.audio_enabled(req.conn, req.actor.org_id)
    return ('<h2>会話から次の業務を考える機能（オーナーの設定・既定はオフ）</h2>'
            '<div class="card"><p>作り手が、会話の相手に伝えたうえで、会話を<b>文字にして確認</b>し、その文字だけを AI（Claude）へ渡します。'
            '音声そのものは、保存せず、AI にも渡しません（文字にするのは端末の中）。AI は次の業務を<b>提案</b>するだけで、作り手が「合っている」と答え、'
            '承認したものだけが、<b>下書き</b>になります（送信・公開・共有範囲の変更はしません）。会話は個人情報を含みやすいので、'
            '文字は7日で削除します。</p>'
            f'<p>いまの設定: <b>{"オン" if on else "オフ"}</b></p>'
            f'<form method="post" action="/settings/talk"><input type="hidden" name="enabled" value="{0 if on else 1}">'
            f'<button>{"オフにする" if on else "オンにする"}</button></form>'
            '<p style="margin-top:1em"><b>音声分析（音声を、音声対応のモデルへ渡す経路）</b>: 口調・迷いなども拾えますが、'
            '<b>音声が Google など第三者の提供元へ渡ります</b>（声は個人を識別でき、相手の声も含まれます）。短い区切りだけ・保存しない・'
            '通話の画面の写真があるときは使えません。作り手は、セッションごとに確認します。</p>'
            f'<p>音声分析の設定: <b>{"オン" if audio else "オフ"}</b></p>'
            f'<form method="post" action="/settings/talk-audio"><input type="hidden" name="enabled" value="{0 if audio else 1}">'
            f'<button {"" if on else "disabled"}>{"オフにする" if audio else "オンにする"}</button></form></div>')


def _set_org_flag(req: Req, col: str, value: int, action: str) -> None:
    assert col in ("talk_llm", "talk_audio", "sensitive_industry", "fusion_demo")
    db.run(req.conn, f"INSERT INTO org_setting(org_id, external_llm, {col}) VALUES(?,1,?) "
                     f"ON CONFLICT(org_id) DO UPDATE SET {col}=excluded.{col}", (req.actor.org_id, value))
    db.audit(req.conn, req.actor.org_id, action, req.actor.member_id, req.actor.org_id, "オン" if value else "オフ")
    req.conn.commit()


def settings_talk(req: Req) -> Response:
    if (r := _need_login(req)):
        return r
    if not authz.can(req.actor, "manage_members", {"org_id": req.actor.org_id}):
        return not_found()
    value = 1 if req.form.get("enabled") == "1" else 0
    _set_org_flag(req, "talk_llm", value, "org.talk_llm")
    if not value:  # 会話の機能を切ったら、音声分析も切る
        _set_org_flag(req, "talk_audio", 0, "org.talk_audio")
    return Response(303, headers=[("Location", "/")])


def settings_talk_audio(req: Req) -> Response:
    if (r := _need_login(req)):
        return r
    if not authz.can(req.actor, "manage_members", {"org_id": req.actor.org_id}):
        return not_found()
    value = 1 if req.form.get("enabled") == "1" and talk.enabled(req.conn, req.actor.org_id) else 0
    _set_org_flag(req, "talk_audio", value, "org.talk_audio")
    return Response(303, headers=[("Location", "/")])


SOURCE_LABEL = {"typed": "手入力", "dictation": "入力ツール（Aqua Voice など）で、下の欄に話す", "mic": "この端末のマイク（端末内で文字にする）",
                "screen_audio": "PC の画面・タブの音声（相手の声・端末内で文字にする）",
                "audio_analysis": "音声分析（音声を Google など第三者のモデルへ渡す）"}
WHO_LABEL = ("相手", "作り手", "不明")
STEP_LABEL = {"draft_notification": "通知の下書き", "propose_next_check": "次回点検日の登録", "prefill_card": "新しいカードの入力欄の事前入力"}
PLAN_STATUS_LABEL = {"agreed": "合っている", "disagreed": "違う", "partial": "一部違う", "executed": "合っている（実行済み）"}
READ_LABEL = {"read_records": "その物の記録", "read_screen_result": "画面の分析結果"}


def _talk_back(obj_id: str, sid: str = "") -> Response:
    return Response(303, headers=[("Location", f"/o/{obj_id}/talk" + (f"?s={sid}" if sid else ""))])


def _plan_html(p) -> str:
    st = p["status"]
    head = (f'<div class="card ai"><span class="muted">{esc(_fmt_time(p["created_at"]))}・{esc(p["model"] or "")}・'
            f'{"ループ" if p["mode"] == "loop" else "単発"}・反復{p["iterations"] or 0}回</span>')
    if st == "running":
        return head + '<p>AI が考えています。自動で更新されます…</p></div>'
    if st == "failed":
        return head + '<p class="muted">AI は、検査を通る提案を出せませんでした。会話には影響しません。文字を直すか、ご自身で次の業務を入力してください。</p></div>'
    r = json.loads(p["result"] or "{}")
    if st == "none":
        return head + f'<p>AI の判断: 次の業務は要らない（{esc(r.get("reason", ""))}）</p></div>'
    base = f"/talk/plan/{esc(p['plan_id'])}"
    body = ""
    if r.get("tool") == "ask_clarifying_question":
        body = f'<p><b>AI からの質問:</b> {esc(r["question"])}<br><span class="muted">{esc(r.get("why", ""))}</span></p>'
        if st == "proposed":
            body += (f'<form method="post" action="{base}/respond"><input type="hidden" name="verdict" value="answer">'
                     '<label>答え</label><input name="correction" maxlength="300" required><button>答える</button></form>')
        elif st == "answered":
            body += f'<p class="muted">答え: {esc(p["feedback"] or "")}</p>'
    else:
        body = f'<p><b>私はこう理解しました:</b> {esc(r.get("understanding", ""))}</p>'
        if r.get("mismatch_note"):
            body += f'<p>⚠ 会話と画面の食い違い: {esc(r["mismatch_note"])}</p>'
        done = json.loads(p["executed"] or "[]")
        body += "<ul>"
        for i, x in enumerate(r.get("steps", [])):
            body += (f'<li><b>{esc(STEP_LABEL.get(x["kind"], x["kind"]))}</b>: {esc(x["summary"])}'
                     f'<br><span class="muted">根拠（会話から）: 「{esc(x["evidence"])}」／確信度: {esc(x["confidence"])}</span><br>{esc(x["detail"])}')
            if st in ("agreed", "executed") and i not in done:
                cands = []
                if x["kind"] == "propose_next_check":  # AI は日付の言い方までしか書けない。コードが、今日の日付から候補を作って見せる（確定は作り手）
                    cands = jadate.candidates(f'{x["detail"]} {x["summary"]} {x["evidence"]}', dt.date.fromtimestamp(db.now()))
                first = cands[0]["date"] if cands else ""
                extra = f'<input type="date" name="date" required value="{esc(first)}">' if x["kind"] == "propose_next_check" else ""
                label = "この日付で承認して登録する" if x["kind"] == "propose_next_check" else "承認して、下書きにする"
                if cands:
                    body += '<span class="muted">日付の候補（今日の日付から計算・確認してください）:</span>'
                    for c in cands:
                        body += (f'<form method="post" action="{base}/execute/{i}"><input type="hidden" name="date" value="{esc(c["date"])}">'
                                 f'<button>{esc(c["label"])}で承認して登録する</button></form>')
                body += f'<form method="post" action="{base}/execute/{i}">{extra}<button>{label}</button></form>'
            elif i in done:
                body += ' <span class="muted">（実行済み）</span>'
            body += "</li>"
        body += "</ul>"
        if st == "proposed":
            body += (f'<form method="post" action="{base}/respond"><label>この理解は、合っていますか？</label>'
                     '<button name="verdict" value="agree">合っている</button>'
                     '<label>違う・一部違うときは、どう違うかを書いてください</label><input name="correction" maxlength="300">'
                     '<button name="verdict" value="partial">一部違う</button><button name="verdict" value="disagree">違う</button></form>')
        else:
            body += f'<p class="muted">あなたの応答: {esc(PLAN_STATUS_LABEL.get(st, st))}' + (f'（{esc(p["feedback"])}）' if p["feedback"] else "") + "</p>"
    if st in ("disagreed", "partial", "answered"):
        body += f'<form method="post" action="{base}/revise"><button>この答えを材料に、AI に考え直してもらう</button></form>'
    reads = [t["read"] for t in json.loads(p["trace"] or "[]") if t.get("read")]
    if reads:
        body += f'<p class="muted">AI が調べたこと: {esc("・".join(READ_LABEL.get(x, x) for x in reads))}</p>'
    return head + body + "</div>"


def talk_page(req: Req, obj_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    obj = objects.get_object_for(req.conn, req.actor, obj_id)
    if not talk.enabled(req.conn, req.actor.org_id):
        return page("会話から次の業務を考える", "<p>この機能は、この組織ではオフです（オーナーが設定します）。</p>", 403)
    sid = req.query.get("s", "")
    back = f'<p><a href="/o/{esc(obj_id)}">物のページへ</a></p>'
    if not sid:
        recent = db.many(req.conn, "SELECT session_id, created_at, source FROM talk_session WHERE org_id=? AND obj_id=? AND actor_id=? "
                                   "ORDER BY created_at DESC LIMIT 5", (req.actor.org_id, obj_id, req.actor.member_id))
        items = "".join(f'<li><a href="/o/{esc(obj_id)}/talk?s={esc(x["session_id"])}">{esc(_fmt_time(x["created_at"]))}・{esc(SOURCE_LABEL.get(x["source"], ""))}</a></li>' for x in recent)
        audio = talk.audio_enabled(req.conn, req.actor.org_id)
        opts = "".join(f'<option value="{k}">{esc(v)}</option>' for k, v in SOURCE_LABEL.items() if k != "audio_analysis" or audio)
        return page(f"会話から次の業務を考える: {obj['name']}",
                    '<p class="muted">会話の相手に伝えたうえで、会話を文字にして、確認してから、AI に考えてもらいます。始めた間だけ動きます。常時の集音はしません。</p>'
                    f'<form method="post" action="/o/{esc(obj_id)}/talk/start"><label>会話の取り込み方</label><select name="source">{opts}</select>'
                    '<label><input type="checkbox" name="consent" value="1" required>会話の相手に、話を文字にして AI に考えさせることを、伝えました</label>'
                    '<p class="muted">音声分析を選ぶと、<b>音声が Google など第三者の提供元へ渡ります</b>（保存はしません）。</p>'
                    f'<button>始める</button></form><h2>最近の会話</h2><ul>{items or "<li>まだありません</li>"}</ul>{back}')
    s = db.one(req.conn, "SELECT * FROM talk_session WHERE org_id=? AND session_id=?", (req.actor.org_id, sid))
    if s is None or s["obj_id"] != obj_id or not talk.may_use(req.actor, s):
        return not_found()
    segs = talk.segments(req.conn, sid)
    rows = "".join(
        f'<div class="card"><form method="post" action="/talk/{esc(sid)}/edit"><input type="hidden" name="seg" value="{esc(g["seg_id"])}">'
        f'<span class="muted">{esc(g["who"])}{"・確認済み" if g["reviewed"] else "・未確認"}</span>'
        f'<input name="text" value="{esc(g["text"])}" maxlength="{talk.MAX_TEXT}">'
        '<button name="op" value="save">修正して保存</button><button name="op" value="delete">この行を削除</button></form></div>' for g in segs)
    who_opts = "".join(f"<option>{w}</option>" for w in WHO_LABEL)
    unreviewed = any(not g["reviewed"] for g in segs)
    plans = db.many(req.conn, "SELECT * FROM talk_plan WHERE session_id=? ORDER BY created_at DESC", (sid,))
    running = any(p["status"] == "running" and db.now() - p["created_at"] < 180 for p in plans) or s["audio_pending"] > 0
    src = s["source"]
    cap = ""
    if src in ("mic", "screen_audio", "audio_analysis"):
        note = ("音声を、Google など第三者の提供元へ送って文字にします（保存はしません）。" if src == "audio_analysis"
                else "音声は、この端末の中だけで文字にします（外へ送りません）。")
        cap = (f'<div class="card" data-talk data-session="{esc(sid)}" data-source="{esc(src)}"><b>集音</b><p class="muted">{note}</p>'
               '<button type="button" id="talk-toggle">話す（開始）</button><p id="talk-status" class="muted" role="status"></p></div>'
               '<script src="/static/talk.js" defer></script>')
    plan_html = "".join(_plan_html(p) for p in plans)
    hint = "<p class=muted>未確認の区切りがあります。確認するまで、AI には渡りません。</p>" if unreviewed else ""
    resp = page(f"会話から次の業務を考える: {obj['name']}",
                f'<p class="muted">出どころ: {esc(SOURCE_LABEL.get(src, src))}</p>{cap}'
                f'<h2>会話の文字（確認してから、AI に渡します）</h2>{rows or "<p class=muted>まだ文字がありません。</p>"}'
                f'<form method="post" action="/talk/{esc(sid)}/add"><label>文字を足す（手入力・Aqua Voice などで話した文字）</label>'
                f'<select name="who">{who_opts}</select><textarea name="text" rows="2" maxlength="{talk.MAX_TEXT}"></textarea><button>足す</button></form>'
                f'<form method="post" action="/talk/{esc(sid)}/confirm"><button {"" if segs else "disabled"}>この文字で間違いないことを確認した</button></form>{hint}'
                f'<form method="post" action="/talk/{esc(sid)}/analyze"><label>AI の考え方</label>'
                '<select name="mode"><option value="single">単発（1回で考える）</option><option value="loop">ループ（必要なときだけ記録を調べ直して考える）</option></select>'
                '<p class="muted">AI に渡るのは、確認した文字と、（あれば）通話の画面の分析結果の文字・この物の記録です。画像・音声は渡りません。</p>'
                f'<button {"" if segs and not unreviewed else "disabled"}>AI に、次の業務を考えてもらう</button></form>'
                f'<h2>AI の提案</h2>{plan_html or "<p class=muted>まだありません。</p>"}{back}',
                head='<meta http-equiv="refresh" content="3">' if running else "")
    if src in ("mic", "screen_audio", "audio_analysis"):
        resp.permissions = "display-capture=(self), camera=(), microphone=(self), geolocation=()"
    return resp


def _talk_session_obj(req: Req, sid: str) -> str:
    s = db.one(req.conn, "SELECT obj_id FROM talk_session WHERE org_id=? AND session_id=?", (req.actor.org_id, sid)) if req.actor.org_id else None
    if s is None:
        raise objects.NotFound(sid)
    return s["obj_id"]


def talk_start(req: Req, obj_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    sid = talk.create_session(req.conn, req.actor, obj_id, source=req.form.get("source", ""), consent=req.form.get("consent") == "1")
    return _talk_back(obj_id, sid)


def talk_add(req: Req, sid: str) -> Response:
    if (r := _need_login(req)):
        return r
    talk.add_segments(req.conn, req.actor, sid, [{"who": req.form.get("who", "不明"), "text": req.form.get("text", "")}])
    return _talk_back(_talk_session_obj(req, sid), sid)


def talk_edit(req: Req, sid: str) -> Response:
    if (r := _need_login(req)):
        return r
    talk.edit_segment(req.conn, req.actor, sid, req.form.get("seg", ""), text=req.form.get("text"), delete=req.form.get("op") == "delete")
    return _talk_back(_talk_session_obj(req, sid), sid)


def talk_confirm(req: Req, sid: str) -> Response:
    if (r := _need_login(req)):
        return r
    talk.confirm_segments(req.conn, req.actor, sid)
    return _talk_back(_talk_session_obj(req, sid), sid)


def talk_analyze(req: Req, sid: str) -> Response:
    if (r := _need_login(req)):
        return r
    mode = req.form.get("mode") if req.form.get("mode") in ("loop", "single") else None
    talk_loop.start(req.conn, req.actor, sid, jobs=JOBS, client_factory=CLIENT_FACTORY, mode=mode)
    return _talk_back(_talk_session_obj(req, sid), sid)


def talk_audio_upload(req: Req, sid: str) -> Response:
    """音声分析の、音声 1 区切り（短い）。条件を、音声を外へ出す前に確かめる。音声は保存しない。"""
    if (r := _need_login(req)):
        return r
    data = next((d for n, d in req.files if n == "audio"), b"")
    fmt = req.form.get("format", "wav")
    talk_audio.precheck(req.conn, req.actor, sid, data, fmt)
    JOBS.submit(req.conn, lambda job_conn: talk_audio.analyze(job_conn, req.actor, sid, data, fmt, client_factory=CLIENT_FACTORY))
    return Response(200, b"ok", content_type="text/plain; charset=utf-8")


def _plan_obj_sid(req: Req, plan_id: str) -> tuple[str, str]:
    p = talk.plan_of(req.conn, req.actor, plan_id)
    return _talk_session_obj(req, p["session_id"]), p["session_id"]


def talk_respond(req: Req, plan_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    talk.respond(req.conn, req.actor, plan_id, req.form.get("verdict", ""), req.form.get("correction", ""))
    return _talk_back(*_plan_obj_sid(req, plan_id))


def talk_revise(req: Req, plan_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    talk_loop.revise(req.conn, req.actor, plan_id, jobs=JOBS, client_factory=CLIENT_FACTORY)
    return _talk_back(*_plan_obj_sid(req, plan_id))


def talk_execute(req: Req, plan_id: str, idx: str) -> Response:
    if (r := _need_login(req)):
        return r
    out = talk.execute_step(req.conn, req.actor, plan_id, int(idx), date=req.form.get("date", ""))
    if out.get("url"):
        return Response(303, headers=[("Location", out["url"])])
    if out.get("notif_id"):
        return Response(303, headers=[("Location", "/n")])  # 承認待ちの下書きの一覧へ（送信はしない）
    return _talk_back(*_plan_obj_sid(req, plan_id))


# ---- 統合分析（音声×共有画面→資料・メール下書き。デモ向け・既定オフ。app/fusion.py）----------------------------

def _fusion_setting_block(req: Req) -> str:
    if not authz.can(req.actor, "manage_members", {"org_id": req.actor.org_id}):
        return ""
    row = db.one(req.conn, "SELECT fusion_demo, fusion_share_dir FROM org_setting WHERE org_id=?", (req.actor.org_id,))
    on = bool(row and row["fusion_demo"])
    d = (row["fusion_share_dir"] if row else "") or ""
    ready = fusion.enabled(req.conn, req.actor.org_id)
    return ('<h2>統合分析（デモ向け・オーナーの設定・既定はオフ）</h2>'
            '<div class="card"><p>会話の音声と、通話の画面（フィルター後）を突き合わせ、表（Excel）・文書（Word）・メールの下書きを作ります。'
            '<b>音声と、フィルター後の画像の両方が、AI の提供元（Orca 経由の Google・Anthropic）へ渡ります。</b>'
            '使うには、上の「画像を AI に見せる機能」と「音声分析」も、オンにする必要があります。作り手が、そのつど確認して押したときだけ動きます。'
            '共有・送信は、AI は実行しません（作り手が押したときだけ）。</p>'
            f'<p>いまの設定: <b>{"オン" if on else "オフ"}</b>{"（3 つの条件がそろっています）" if ready else "（まだ使えません: 3 つの条件がそろっていません）" if on else ""}</p>'
            f'<form method="post" action="/settings/fusion"><input type="hidden" name="enabled" value="{0 if on else 1}">'
            f'<button>{"オフにする" if on else "オンにする"}</button></form>'
            '<form method="post" action="/settings/fusion-dir"><label>共有先のフォルダ（サーバーにある、絶対パス。空にすると解除）</label>'
            f'<input name="dir" value="{esc(d)}" maxlength="300"><button>共有先を保存</button></form></div>')


def _gmail_pref_block(req: Req) -> str:
    """送信の準備（Gmail の作成画面）で使う、自分の Gmail アドレスの設定。メンバー本人だけ。任意。"""
    if not fusion.enabled(req.conn, req.actor.org_id):
        return ""
    cur = fusion.get_gmail(req.conn, req.actor)
    return ('<h2>自分の Gmail アドレス（任意）</h2><div class="card">'
            '<p>統合分析の「送信の準備」で開く Gmail の作成画面に、ここで設定した<b>自分のアドレス</b>を、宛先（To）と、開くアカウントとして入れます。'
            '未設定なら、宛先なしで開きます。設定できるのは、gmail.com のアドレスだけです。送信は、Gmail であなたが行います。</p>'
            f'<form method="post" action="/settings/gmail"><label>Gmail アドレス（空にすると解除）</label>'
            f'<input name="address" type="email" value="{esc(cur)}" maxlength="120" placeholder="you@gmail.com" autocomplete="email">'
            '<button>保存</button></form></div>')


def settings_gmail(req: Req) -> Response:
    if (r := _need_login(req)):
        return r
    fusion.set_gmail(req.conn, req.actor, req.form.get("address", ""))
    return Response(303, headers=[("Location", "/")])


def settings_fusion(req: Req) -> Response:
    if (r := _need_login(req)):
        return r
    if not authz.can(req.actor, "manage_members", {"org_id": req.actor.org_id}):
        return not_found()
    _set_org_flag(req, "fusion_demo", 1 if req.form.get("enabled") == "1" else 0, "org.fusion_demo")
    return Response(303, headers=[("Location", "/")])


def settings_fusion_dir(req: Req) -> Response:
    if (r := _need_login(req)):
        return r
    if not authz.can(req.actor, "manage_members", {"org_id": req.actor.org_id}):
        return not_found()
    fusion.set_share_dir(req.conn, req.actor, req.form.get("dir", ""))
    return Response(303, headers=[("Location", "/")])


def _fusion_table_html(t: dict) -> str:
    head = "".join(f"<th>{esc(c)}</th>" for c in t["columns"])
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in t["rows"])
    return f'<div class="tblwrap"><table><tr>{head}</tr>{body}</table></div>'


def _fusion_result_html(req: Req, card, f: dict) -> str:
    r, fid = f["result"], f["id"]
    step = lambda n, title, inner: f'<div class="card step"><span class="n">{n}</span><div><b>{esc(title)}</b>{inner}</div></div>'
    heard = "<br>".join(f'<span class="muted">[{esc(t["who"])}]</span> {esc(t["text"])}' for t in f["transcript"])
    seen = f'<p>{esc(r["understanding"])}</p>'
    for tgt in r.get("targets", []):
        seen += f'<p>対象物: <b>{esc(tgt["label"])}</b> <span class="muted">（確信度: {esc(tgt["confidence"])}／未確認）</span>' \
                + (f'<br><span class="muted">画像で見えている根拠: {esc(tgt["visible_basis"])}</span>' if tgt.get("visible_basis") else "") + "</p>"
    if "circled.jpg" in f["files"]:
        seen += f'<img src="/f/{esc(fid)}/file/circled.jpg" alt="対象物に赤丸をつけた、フィルター後の画像">'
    elif r.get("targets"):
        seen += '<p class="muted">対象物の位置は、確かでないため、赤丸をつけていません。</p>'
    made = (f'<p><b>{esc(r["table"]["title"])}</b></p>{_fusion_table_html(r["table"])}'
            f'<p><b>{esc(r["report"]["title"])}</b></p>'
            + "".join(f'<p><b>{esc(s["heading"])}</b><br>{"<br>".join(esc(x) for x in s["paragraphs"])}</p>' for s in r["report"]["sections"])
            + f'<p><b>メールの下書き</b>（宛先なし）<br>件名: {esc(r["email"]["subject"])}<br>{esc(r["email"]["body"]).replace(chr(10), "<br>")}</p>'
            f'<div class="choices"><a class="btn sub" href="/f/{esc(fid)}/file/table.xlsx">表（Excel）をダウンロード</a>'
            f'<a class="btn sub" href="/f/{esc(fid)}/file/report.docx">文書（Word）をダウンロード</a></div>')
    d = fusion.share_dir(req.conn, req.actor.org_id)
    hint = d.name if d else ""
    choices = ""
    if d:
        choices += "".join(f'<form method="post" action="/f/{esc(fid)}/share/{n}"><button class="sub">{lab}を共有フォルダへコピー</button></form>'
                           for n, lab in (("table.xlsx", "表"), ("report.docx", "文書")))
    choices += f'<a class="btn" href="{esc(fusion.gmail_url(r["email"], hint, fusion.get_gmail(req.conn, req.actor)))}" target="_blank" rel="noopener noreferrer">送信の準備（Gmailで編集して送る）</a>'
    nxt = "".join(f'<li>{esc(n["label"])}<br><span class="muted">{esc(n["reason"])}</span></li>' for n in r.get("next_work", []))
    if r.get("date_candidates"):
        nxt += "".join(f'<li class="muted">日付の候補: {esc(c)}</li>' for c in r["date_candidates"])
    after = (f'<div class="choices">{choices}</div>'
             '<p class="muted">「共有」は、あなたが押したときだけ、オーナーが決めたフォルダへコピーします。「送信の準備」は、Gmail の作成画面を開くだけで、'
             '送信はあなたが Gmail で行います。AI は、共有も送信もしません。</p>' + (f"<p>この対象物からの、次の作業の選択肢（提案）</p><ul>{nxt}</ul>" if nxt else ""))
    return ('<div class="flow">' + step(1, "聞いたこと（確認済みの文字）", f"<p>{heard}</p>") + step(2, "見えたもの・私はこう理解しました", seen)
            + step(3, "作ったもの（下書き）", made) + step(4, "次の選択肢", after) + "</div>"
            f'<p class="muted">モデル: {esc(f["model"] or "")}／費用: ${f["cost_usd"] or 0:.4f}／AI の答えは提案です。'
            + (f'検査で調整した点: {esc(f["why"])}' if f["why"] else "") + "</p>")


def _fusion_block(req: Req, card, imgs) -> tuple[str, bool]:
    """カードのページに、統合分析の操作と結果を出す。作り手（編集できる人）だけ。3 つの条件がそろわないときは、何も出さない。"""
    if not vision.may_start(req.actor, card) or not fusion.enabled(req.conn, req.actor.org_id):
        return "", False
    calls = [i for i in imgs if i["source"] == "call_screen"]
    if not calls:
        return "", False
    fid = fusion.latest_for_card(req.conn, req.actor, card["card_id"])
    f = fusion.get(req.conn, req.actor, fid) if fid else None
    row = db.one(req.conn, "SELECT created_at FROM fusion_run WHERE fusion_id=?", (fid,)) if fid else None
    fresh = bool(row and db.now() - row["created_at"] < 240)
    running, body = False, ""
    if f and f["status"] in ("transcribing", "analyzing") and fresh:
        running = True
        body += f'<p class="thinking card">{"音声を文字にしています" if f["status"] == "transcribing" else "会話と画面を突き合わせて、資料を作っています"}。自動で更新されます…</p>'
    elif f and f["status"] == "transcribed":
        rows = "".join(f'<label>{esc(t["who"])}</label><textarea name="t{k}" rows="2" maxlength="500">{esc(t["text"])}</textarea>' for k, t in enumerate(f["transcript"]))
        body += (f'<p><b>音声を文字にしました。間違いがあれば直してください。</b>（この確認が終わるまで、画像は AI に渡しません。空にした行は使いません）</p>'
                 f'<form method="post" action="/f/{esc(f["id"])}/confirm">{rows}<button>この文字で、統合分析する</button></form>')
    elif f and f["status"] == "done" and f["result"]:
        return f'<div class="card ai"><b>統合分析（会話×共有画面）</b>{_fusion_result_html(req, card, f)}</div>', False
    else:
        if f and f["status"] in ("failed", "rejected", "transcribing", "analyzing"):
            body += f'<p class="muted">前回の統合分析は、採用されませんでした（{esc(f["why"] or "時間切れ")}）。カードには影響しません。</p>'
        demo = fusion.demo_audio() is not None
        i = calls[0]
        body += (f'<form method="post" enctype="multipart/form-data" action="/c/{esc(card["card_id"])}/fusion/{esc(i["image_id"])}">'
                 '<label>会話の音声（wav または mp3・約 45 秒まで）</label><input type="file" name="audio" accept=".wav,.mp3,audio/wav,audio/mpeg">'
                 '<label>形式</label><select name="format"><option value="wav">wav</option><option value="mp3">mp3</option></select>'
                 '<label><input type="checkbox" name="consent" value="1" required>この音声と、フィルター後の画像（作り手が開けた所だけ）が、'
                 'AI の提供元（Orca 経由の Google・Anthropic）へ渡ることを確認しました</label>'
                 '<button>音声を文字にして、統合分析を始める</button>'
                 + ('<button name="demo" value="1" class="sub">デモ用ボイスで始める</button>' if demo else "") + "</form>")
    return f'<div class="card ai"><b>統合分析（会話×共有画面）</b>{body}</div>', running


def card_fusion_start(req: Req, card_id: str, image_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    audio = next((d for n, d in req.files if n == "audio"), b"")
    fmt = req.form.get("format", "wav")
    if req.form.get("demo") == "1":
        audio, fmt = (fusion.demo_audio() or (b"", "wav"))
    try:
        fusion.start(req.conn, req.actor, card_id, image_id, audio, fmt, consent=req.form.get("consent") == "1", jobs=JOBS, client_factory=CLIENT_FACTORY)
    except fusion.FusionRefused as e:
        return page("統合分析を始められません", f"<p>{esc(str(e))}</p><p><a href=\"/c/{esc(card_id)}\">カードへ戻る</a></p>", 400)
    except vision.VisionRefused as e:
        return page("統合分析を始められません", f"<p>{esc(str(e))}</p><p><a href=\"/c/{esc(card_id)}\">カードへ戻る</a></p>", 400)
    return Response(303, headers=[("Location", f"/c/{card_id}")])


def fusion_confirm(req: Req, fusion_id: str) -> Response:
    if (r := _need_login(req)):
        return r
    f = fusion.get(req.conn, req.actor, fusion_id)
    edited = [req.form.get(f"t{k}", "") for k in range(len(f["transcript"]))]
    try:
        fusion.confirm_and_analyze(req.conn, req.actor, fusion_id, edited, jobs=JOBS, client_factory=CLIENT_FACTORY)
    except (fusion.FusionRefused, vision.VisionRefused) as e:
        return page("統合分析を進められません", f"<p>{esc(str(e))}</p><p><a href=\"/c/{esc(f['card_id'])}\">カードへ戻る</a></p>", 400)
    return Response(303, headers=[("Location", f"/c/{f['card_id']}")])


def fusion_file(req: Req, fusion_id: str, name: str) -> Response:
    if (r := _need_login(req)):
        return r
    mime, data = fusion.download(req.conn, req.actor, fusion_id, name)
    hdr = [] if mime == "image/jpeg" else [("Content-Disposition", f'attachment; filename="{name}"')]
    return Response(200, data, hdr, content_type=mime)


def fusion_share(req: Req, fusion_id: str, name: str) -> Response:
    if (r := _need_login(req)):
        return r
    f = fusion.get(req.conn, req.actor, fusion_id)
    try:
        dest = fusion.share(req.conn, req.actor, fusion_id, name)
    except fusion.FusionRefused as e:
        return page("共有できません", f"<p>{esc(str(e))}</p><p><a href=\"/c/{esc(f['card_id'])}\">カードへ戻る</a></p>", 400)
    return page("共有しました", f"<p>共有フォルダへコピーしました: {esc(dest)}</p><p><a href=\"/c/{esc(f['card_id'])}\">カードへ戻る</a></p>")


ROUTES = [
    ("POST", r"/settings/gmail", settings_gmail), ("POST", r"/settings/fusion", settings_fusion), ("POST", r"/settings/fusion-dir", settings_fusion_dir),
    ("POST", r"/c/([\w\-]+)/fusion/([\w\-]+)", card_fusion_start), ("POST", r"/f/([\w\-]+)/confirm", fusion_confirm),
    ("GET", r"/f/([\w\-]+)/file/([\w.\-]+)", fusion_file), ("POST", r"/f/([\w\-]+)/share/([\w.\-]+)", fusion_share),
    ("GET", r"/o/([\w\-]+)/talk", talk_page), ("POST", r"/o/([\w\-]+)/talk/start", talk_start),
    ("POST", r"/talk/([\w\-]+)/add", talk_add), ("POST", r"/talk/([\w\-]+)/edit", talk_edit),
    ("POST", r"/talk/([\w\-]+)/confirm", talk_confirm), ("POST", r"/talk/([\w\-]+)/analyze", talk_analyze),
    ("POST", r"/talk/([\w\-]+)/audio", talk_audio_upload),
    ("POST", r"/talk/plan/([\w\-]+)/respond", talk_respond), ("POST", r"/talk/plan/([\w\-]+)/revise", talk_revise),
    ("POST", r"/talk/plan/([\w\-]+)/execute/(\d+)", talk_execute),
    ("POST", r"/settings/talk", settings_talk), ("POST", r"/settings/talk-audio", settings_talk_audio),
    ("POST", r"/o/([\w\-]+)/options", object_options),
    ("POST", r"/settings/external-llm", settings_external_llm), ("POST", r"/settings/vision-ai", settings_vision_ai), ("POST", r"/settings/sensitive-industry", settings_sensitive_industry),
    ("POST", r"/c/([\w\-]+)/vision/([\w\-]+)", card_vision_start),
    ("GET", r"/n", notifications_page), ("POST", r"/n/([\w\-]+)/approve", notification_approve),
    ("POST", r"/n/([\w\-]+)/reject", notification_reject),
    ("GET", r"/static/([\w.\-]+)", static_file),
    ("GET", r"/c/([\w\-]+)/mask/([\w\-]+)", mask_edit_page), ("POST", r"/c/([\w\-]+)/mask/([\w\-]+)", mask_edit_save),
    ("GET", r"/", home), ("GET", r"/login", login_form), ("POST", r"/login", login_post),
    ("POST", r"/login/verify", login_verify), ("POST", r"/logout", logout),
    ("POST", r"/objects", object_create), ("GET", r"/t/([\w\-]+)", tag_page),
    ("GET", r"/o/([\w\-]+)", object_page), ("GET", r"/o/([\w\-]+)/new", card_new),
    ("POST", r"/o/([\w\-]+)/cards", card_create), ("GET", r"/c/([\w\-]+)", card_page),
    ("POST", r"/c/([\w\-]+)/narrow", card_narrow), ("POST", r"/c/([\w\-]+)/mask-ack", card_mask_ack), ("POST", r"/c/([\w\-]+)/share", card_share),
    ("GET", r"/s/([\w\-]+)", share_view), ("GET", r"/img/([\w\-]+)", image_shared),
    ("GET", r"/mimg/([\w\-]+)", image_member),
]


def _same_origin(headers: dict) -> bool:
    origin = headers.get("origin")
    if not origin:
        return True  # ブラウザは、別サイトからの POST に必ず Origin を付ける。Cookie も SameSite=Strict
    return urllib.parse.urlsplit(origin).netloc == headers.get("host", "")


def body_limit(method: str, path: str, headers: dict) -> int:
    """写真つきのカード作成だけ大きな本文を許す。ほかは 64KB。"""
    if method == "POST" and re.fullmatch(r"/o/[\w\-]+/cards|/talk/[\w\-]+/audio|/c/[\w\-]+/fusion/[\w\-]+", path) and \
            headers.get("content-type", "").startswith("multipart/form-data"):
        return MAX_UPLOAD
    return MAX_BODY


def parse_multipart(content_type: str, body: bytes) -> tuple[dict, list]:
    msg = email.message_from_bytes(b"Content-Type: " + content_type.encode("latin-1", "replace") + b"\r\n\r\n" + body)
    if not msg.is_multipart():
        raise ValueError("フォームの形式が正しくありません")
    fields, files = {}, []
    for part in msg.get_payload():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            files.append((name, data))
        elif name:
            fields[name] = data.decode("utf-8", "replace")
    if len(files) > len(PHOTO_PARTS):
        raise ValueError("写真が多すぎます")
    return fields, files


def handle(conn, method: str, target: str, headers: dict | None = None, body: bytes = b"", ip: str = "-") -> Response:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    u = urllib.parse.urlsplit(target)
    form, files = {}, []
    if method == "POST":
        limit = body_limit(method, u.path, headers)
        if len(body) > limit:
            return page("大きすぎます", "", 413)
        if not _same_origin(headers):
            return page("拒否しました", "", 403)
        try:
            if limit == MAX_UPLOAD:
                form, files = parse_multipart(headers["content-type"], body)
            else:
                form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}
        except ValueError as e:
            return page("入力を確認してください", f"<p>{esc(str(e))}</p>", 400)
    ck = cookies.SimpleCookie()
    try:
        ck.load(headers.get("cookie", ""))
    except cookies.CookieError:
        pass
    req = Req(conn, method, u.path, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}, form, files, headers,
              {k: m.value for k, m in ck.items()}, ip)
    _load_actor(req)
    for m, pattern, fn in ROUTES:
        hit = re.fullmatch(pattern, u.path)
        if m == method and hit:
            try:
                return fn(req, *hit.groups())
            except objects.NotFound:
                return not_found()
            except objects.Denied:
                return page("権限がありません", "", 403)
            except ValueError as e:
                return page("入力を確認してください", f"<p>{esc(str(e))}</p>", 400)
    return not_found()


# ---- サーバー ----------------------------------------------------------------

def seed_dev(conn, email: str) -> None:
    if db.one(conn, "SELECT 1 FROM org"):
        return
    db.run(conn, "INSERT INTO org VALUES(?,?,?,?)", ("org_dev", "開発用の組織", "info@example.test", db.now()))
    auth.add_member(conn, "org_dev", email, "owner")
    conn.commit()


def _mask(path: str) -> str:
    return re.sub(r"^(/(?:s|t|i))/[^/?]+", r"\1/[…]", path)


def make_handler(conn):
    class Handler(server.BaseHTTPRequestHandler):
        def _run(self):
            n = int(self.headers.get("Content-Length") or 0)
            path = urllib.parse.urlsplit(self.path).path
            limit = body_limit(self.command, path, {k.lower(): v for k, v in self.headers.items()})
            body = self.rfile.read(min(n, limit + 1)) if n else b""
            with _REQUEST_LOCK:
                r = handle(conn, self.command, self.path, dict(self.headers), body, self.client_address[0])
            self.send_response(r.status)
            for k, v in r.all_headers():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(r.body)))
            self.end_headers()
            self.wfile.write(r.body)

        do_GET = do_POST = _run

        def log_message(self, fmt, *args):  # N6: トークンをアクセスログに残さない
            sys.stderr.write(f"{self.command} {_mask(self.path)}\n")

    return Handler


def main() -> None:
    if os.environ.get("MIRUCON_ENV") != "dev":
        sys.exit("開発モード専用です。MIRUCON_ENV=dev を設定してください。")
    os.environ.setdefault("MIRUCON_LLM_PROFILE", "orca")  # Orca 主体・Haiku なし（llm.PROFILES）。従来の表にしたいときは、空にする
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")  # 開発モードのワンタイムコードを表示する
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(DB_PATH)
    db.init(conn)
    if len(sys.argv) > 1:
        seed_dev(conn, sys.argv[1])
    port = int(os.environ.get("PORT", "8000"))
    print(f"http://127.0.0.1:{port}/login （ワンタイムコードは、この端末のログに出ます）")
    global JOBS
    JOBS = jobs.Threaded(lambda: db.connect(DB_PATH))  # AI の処理は別スレッド・専用の接続。カード作成は、待たずに返る
    server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(conn)).serve_forever()


if __name__ == "__main__":
    main()
