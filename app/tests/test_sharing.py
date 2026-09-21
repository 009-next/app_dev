import pytest

from app import db, sharing

ORG = "org_1"


@pytest.fixture(autouse=True)
def _card_c1(conn):
    db.run(conn, "INSERT INTO card(card_id,obj_id,org_id,creator_id,scope,created_at) VALUES('c1','o1',?,'m1','link_30d',?)",
           (ORG, db.now()))
    conn.commit()


def _share(conn):
    return sharing.create_share(conn, ORG, db.get_card(conn, ORG, "c1"), "m1", None)


def _image(conn):
    return {"image_id": "img1", "card_id": "c1"}


def _signed_parts(url):
    q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    return q["u"], q["e"], q["g"]


def test_share_resolves_then_stops_after_revoke(conn):
    s = _share(conn)
    assert sharing.resolve_share(conn, s["token"]) is not None
    assert sharing.revoke_share(conn, ORG, s["share_id"], "m1")
    assert sharing.resolve_share(conn, s["token"]) is None


def test_share_stops_after_expiry(conn):
    s = _share(conn)
    db.shift_clock(31 * sharing.DAY)
    assert sharing.resolve_share(conn, s["token"]) is None


def test_share_stops_when_scope_narrowed(conn):
    s = _share(conn)
    db.run(conn, "UPDATE card SET scope='org_only' WHERE card_id='c1'")
    assert sharing.resolve_share(conn, s["token"]) is None


def test_unknown_token_is_none(conn):
    assert sharing.resolve_share(conn, "s_nope") is None
    assert sharing.resolve_share(conn, "") is None


def test_image_url_valid_then_dead_after_revoke(conn):
    s = _share(conn)
    img = _image(conn)
    u, e, g = _signed_parts(sharing.sign_image(conn, "img1", f"share:{s['share_id']}"))
    assert sharing.verify_image_request(conn, img, u, e, g)
    sharing.revoke_share(conn, ORG, s["share_id"], "m1")
    assert not sharing.verify_image_request(conn, img, u, e, g)


def test_image_url_rejects_tampering_and_expiry(conn):
    s = _share(conn)
    img = _image(conn)
    u, e, g = _signed_parts(sharing.sign_image(conn, "img1", f"share:{s['share_id']}"))
    assert not sharing.verify_image_request(conn, img, u, e, g[:-1] + ("0" if g[-1] != "0" else "1"))
    assert not sharing.verify_image_request(conn, img, u, "notint", g)
    db.shift_clock(sharing.IMG_URL_TTL + 1)
    assert not sharing.verify_image_request(conn, img, u, e, g)


def test_image_url_of_other_card_is_rejected(conn):
    s = _share(conn)
    u, e, g = _signed_parts(sharing.sign_image(conn, "img1", f"share:{s['share_id']}"))
    assert not sharing.verify_image_request(conn, {"image_id": "img1", "card_id": "other"}, u, e, g)


def test_invite_revoke(conn):
    inv = sharing.create_invite(conn, ORG, db.get_card(conn, ORG, "c1"), "m1", "A@Example.com")
    assert inv["email"] == "a@example.com"
    assert sharing.resolve_invite(conn, inv["token"]) is not None
    sharing.revoke_invite(conn, ORG, inv["invite_id"], "m1")
    assert sharing.resolve_invite(conn, inv["token"]) is None
