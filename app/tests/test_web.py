import re
import urllib.parse

import pytest

from app import auth, cards, db, objects, sharing, web
from app.tests.fakes import client_sequence

HOST = {"host": "localhost:8000"}
COMMON = {"reason": "r", "evidence": "エアコン"}
WRITE = ("write_card_text", {"title": "エアコン清掃", "changes": ["フィルターを清掃"], "description": "清掃した"})


@pytest.fixture(autouse=True)
def _limits():
    web.reset_limits()
    auth.reset_rate()
    web.CLIENT_FACTORY = None
    yield
    web.CLIENT_FACTORY = None


def get(conn, path, cookie=None, ip="1.1.1.1"):
    h = dict(HOST)
    if cookie:
        h["cookie"] = cookie
    return web.handle(conn, "GET", path, h, ip=ip)


def post(conn, path, data, cookie=None, headers=None):
    h = {**HOST, **(headers or {})}
    if cookie:
        h["cookie"] = cookie
    return web.handle(conn, "POST", path, h, urllib.parse.urlencode(data).encode())


def login(conn, actor) -> str:
    email = db.get_member(conn, actor.org_id, actor.member_id)["email"]
    code = auth.request_login_code(conn, email)
    r = post(conn, "/login/verify", {"email": email, "code": code})
    assert r.status == 303
    return dict(r.headers)["Set-Cookie"].split(";")[0]


def text(r) -> str:
    return r.body.decode()


@pytest.fixture
def obj(conn, alice):
    return objects.register_object(conn, alice, "空調")


def _shared_card(conn, alice, obj, jpeg, tmp_path, **kw):
    c = cards.create_card(conn, alice, obj[0]["obj_id"], before_desc=kw.get("before", "汚れ"), after_desc="きれい",
                          images_in=[("before", jpeg, None)], mask_confirmed=True, data_dir=tmp_path)["card"]
    return c, cards.issue_share(conn, alice, c["card_id"], confirmed=True)


def test_login_sets_strict_httponly_cookie_and_hides_account_existence(conn, alice):
    email = "alice@example.test"
    r = post(conn, "/login/verify", {"email": email, "code": auth.request_login_code(conn, email)})
    cookie = dict(r.headers)["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    bad = post(conn, "/login/verify", {"email": email, "code": "000000"})
    ghost = post(conn, "/login/verify", {"email": "nobody@example.test", "code": "000000"})
    assert bad.status == ghost.status == 401 and bad.body == ghost.body
    a = post(conn, "/login", {"email": email})
    b = post(conn, "/login", {"email": "nobody@example.test"})
    assert a.status == b.status == 200 and text(a).replace(email, "X") == text(b).replace("nobody@example.test", "X")


def test_member_pages_require_login(conn, alice, obj):
    o, _ = obj
    for path in ["/", f"/o/{o['obj_id']}", f"/o/{o['obj_id']}/new", "/c/card_x", "/mimg/img_x"]:
        r = get(conn, path)
        assert r.status == 303 and dict(r.headers)["Location"] == "/login"


def test_anonymous_tag_shows_only_name_and_contact(conn, alice, obj):
    o, tag = obj
    cards.create_card(conn, alice, o["obj_id"], before_desc="秘密のカード内容")
    r = get(conn, f"/t/{tag['tag_id']}")
    assert r.status == 200 and "空調" in text(r) and "info@example.test" in text(r)
    assert "秘密のカード内容" not in text(r) and "/o/" not in text(r)


def test_unknown_and_disabled_tag_look_identical(conn, alice, obj):
    o, tag = obj
    objects.disable_tag(conn, alice, tag["tag_id"])
    a, b = get(conn, f"/t/{tag['tag_id']}"), get(conn, "/t/t_unknown")
    assert a.status == b.status == 404 and a.body == b.body


def test_member_tag_page_links_to_records(conn, alice, obj):
    o, tag = obj
    r = get(conn, f"/t/{tag['tag_id']}", cookie=login(conn, alice))
    assert f"/o/{o['obj_id']}" in text(r)


def test_share_view_escapes_html_counts_views_and_shows_ai_label(conn, alice, obj, jpeg, tmp_path):
    o, _ = obj
    f = client_sequence([[("select_card_type", {**COMMON, "type_id": "cleaning"})], [("no_action", COMMON)],
                         [("write_card_text", {"title": "<script>alert(1)</script>", "changes": ["<b>x</b>"],
                                               "description": "d"})]])
    c = cards.create_card(conn, alice, o["obj_id"], before_desc="エアコン<img src=x onerror=1>", client_factory=f,
                          images_in=[("before", jpeg, None)], mask_confirmed=True, data_dir=tmp_path)["card"]
    s = cards.issue_share(conn, alice, c["card_id"], confirmed=True)
    r = get(conn, f"/s/{s['token']}")
    body = text(r)
    assert r.status == 200 and "<script>" not in body and "<img src=x" not in body and "&lt;script&gt;" in body
    assert "AIが書いた" in body and "開発" not in body and "テスト工務店" in body
    assert dict(r.all_headers())["Content-Security-Policy"].startswith("default-src 'none'")
    get(conn, f"/s/{s['token']}")
    assert db.one(conn, "SELECT view_count FROM share")["view_count"] == 2


def test_share_view_hides_voice_text_and_creator_identity(conn, alice, obj):
    o, _ = obj
    c = cards.create_card(conn, alice, o["obj_id"], before_desc="a", voice_text="電話番号090-0000-0000")["card"]
    s = cards.issue_share(conn, alice, c["card_id"], confirmed=True)
    body = text(get(conn, f"/s/{s['token']}"))
    assert "090-0000-0000" not in body and "alice@example.test" not in body and alice.member_id not in body


def test_revoked_expired_unknown_share_and_images_all_look_the_same(conn, alice, obj, jpeg, tmp_path):
    o, _ = obj
    c, s = _shared_card(conn, alice, obj, jpeg, tmp_path)
    page = text(get(conn, f"/s/{s['token']}"))
    img_url = re.search(r'src="(/img/[^"]+)"', page).group(1).replace("&amp;", "&")
    assert get(conn, img_url).status == 200 and get(conn, img_url).content_type == "image/jpeg"
    sharing.revoke_share(conn, alice.org_id, s["share_id"], alice.member_id)
    unknown = get(conn, "/s/s_unknown")
    revoked = get(conn, f"/s/{s['token']}")
    assert revoked.status == unknown.status == 404 and revoked.body == unknown.body
    assert get(conn, img_url).status == 404  # 取り消し後は画像も見られない
    assert get(conn, "/img/img_x?u=share:x&e=1&g=z").status == 404


def test_shared_view_is_rate_limited_per_ip(conn):
    codes = [get(conn, "/s/s_nope", ip="9.9.9.9").status for _ in range(62)]
    assert codes[:60] == [404] * 60 and codes[60:] == [429, 429]
    assert get(conn, "/s/s_nope", ip="8.8.8.8").status == 404


def test_post_from_other_origin_and_oversize_are_rejected(conn, alice):
    ck = login(conn, alice)
    r = post(conn, "/objects", {"name": "x"}, cookie=ck, headers={"origin": "http://evil.example"})
    assert r.status == 403 and db.one(conn, "SELECT COUNT(*) c FROM object")["c"] == 0
    big = web.handle(conn, "POST", "/objects", {**HOST, "cookie": ck}, b"name=" + b"a" * (web.MAX_BODY + 1))
    assert big.status == 413


def test_create_card_via_web_runs_agent(conn, alice, obj):
    o, _ = obj
    web.CLIENT_FACTORY = client_sequence([[("select_card_type", {**COMMON, "type_id": "cleaning"})],
                                          [("no_action", COMMON)], [WRITE]])
    ck = login(conn, alice)
    r = post(conn, f"/o/{o['obj_id']}/cards", {"before_desc": "エアコンが汚い", "scope": "org_only"}, cookie=ck)
    assert r.status == 303
    page = text(get(conn, dict(r.headers)["Location"], cookie=ck))
    assert "エアコン清掃" in page and "AIが書いた文章" in page and "組織内のみ" in page


def test_other_org_member_cannot_open_card_or_object(conn, alice, obj, outsider):
    o, _ = obj
    c = cards.create_card(conn, alice, o["obj_id"], scope="org_only")["card"]
    ck = login(conn, outsider)
    assert get(conn, f"/c/{c['card_id']}", cookie=ck).status == 404
    assert get(conn, f"/o/{o['obj_id']}", cookie=ck).status == 404


def test_member_cannot_see_invited_only_card_of_colleague(conn, alice, bob, obj):
    o, _ = obj
    c = cards.create_card(conn, alice, o["obj_id"], scope="invited_only")["card"]
    assert get(conn, f"/c/{c['card_id']}", cookie=login(conn, bob)).status == 404
    assert get(conn, f"/c/{c['card_id']}", cookie=login(conn, alice)).status == 200


def test_share_issue_needs_confirmation_and_recent_login(conn, alice, obj):
    o, _ = obj
    c = cards.create_card(conn, alice, o["obj_id"])["card"]
    ck = login(conn, alice)
    assert post(conn, f"/c/{c['card_id']}/share", {}, cookie=ck).status == 400
    r = post(conn, f"/c/{c['card_id']}/share", {"confirmed": "1"}, cookie=ck)
    assert r.status == 200 and "/s/s_" in text(r)


def test_stale_session_is_asked_to_reauthenticate(conn, alice, obj):
    o, _ = obj
    c = cards.create_card(conn, alice, o["obj_id"])["card"]
    ck = login(conn, alice)
    db.run(conn, "UPDATE session SET last_auth_at=last_auth_at-?", (auth.STEPUP_WINDOW + 5,))
    r = post(conn, f"/c/{c['card_id']}/share", {"confirmed": "1"}, cookie=ck)
    assert r.status == 403 and "再認証" in text(r)


def test_narrow_via_web_and_widen_is_not_exposed(conn, alice, obj):
    o, _ = obj
    c = cards.create_card(conn, alice, o["obj_id"], scope="org_only")["card"]
    ck = login(conn, alice)
    assert post(conn, f"/c/{c['card_id']}/narrow", {"scope": "link_30d"}, cookie=ck).status == 400
    assert post(conn, f"/c/{c['card_id']}/narrow", {"scope": "invited_only"}, cookie=ck).status == 303
    assert cards.get_card(conn, alice, c["card_id"])["scope"] == "invited_only"
    assert web.handle(conn, "POST", f"/c/{c['card_id']}/widen", {**HOST, "cookie": ck}, b"").status == 404


def test_access_log_masks_tokens():
    assert web._mask("/s/s_secret123") == "/s/[…]"
    assert web._mask("/t/t_secret?x=1") == "/t/[…]?x=1"


def test_referrer_policy_keeps_origin_on_same_site_posts_but_never_leaks_urls():
    # no-referrer にすると、ブラウザがフォーム送信に Origin: null を付けて、正当な POST がすべて 403 になる（実ブラウザで発生）
    policy = dict(web.page("x", "").all_headers())["Referrer-Policy"]
    assert policy == "same-origin"
