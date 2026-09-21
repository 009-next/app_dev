import pytest

from app import cards, db, objects


def test_register_object_issues_unguessable_tag_and_audits(conn, alice):
    obj, tag = objects.register_object(conn, alice, "  1階 空調  ")
    assert obj["name"] == "1階 空調" and obj["registrant_id"] == alice.member_id
    assert tag["tag_id"].startswith("t_") and len(tag["tag_id"]) > 20
    assert "空調" not in tag["tag_id"]
    assert db.one(conn, "SELECT COUNT(*) c FROM audit_log WHERE action IN ('object.register','tag.issue')")["c"] == 2


def test_register_rejects_empty_name_and_outsider_assignee(conn, alice, outsider):
    with pytest.raises(ValueError):
        objects.register_object(conn, alice, " ")
    with pytest.raises(objects.NotFound):
        objects.register_object(conn, alice, "x", assignee_id=outsider.member_id)


def test_anonymous_tag_read_shows_only_name_and_contact(conn, alice):
    obj, tag = objects.register_object(conn, alice, "空調")
    cards.create_card(conn, alice, obj["obj_id"], before_desc="a")
    pub = objects.resolve_tag(conn, tag["tag_id"])
    assert pub == {"name": "空調", "contact": "info@example.test"}


def test_member_of_same_org_gets_obj_id_but_other_org_does_not(conn, alice, outsider):
    obj, tag = objects.register_object(conn, alice, "空調")
    assert objects.resolve_tag(conn, tag["tag_id"], alice)["obj_id"] == obj["obj_id"]
    assert "obj_id" not in objects.resolve_tag(conn, tag["tag_id"], outsider)


def test_unknown_or_disabled_tag_is_none(conn, alice):
    _, tag = objects.register_object(conn, alice, "空調")
    assert objects.resolve_tag(conn, "t_nope") is None
    assert objects.resolve_tag(conn, "") is None
    objects.disable_tag(conn, alice, tag["tag_id"])
    assert objects.resolve_tag(conn, tag["tag_id"]) is None


def test_disable_tag_only_owner_or_registrant(conn, alice, bob, owner, outsider):
    _, tag = objects.register_object(conn, alice, "空調")
    with pytest.raises(objects.Denied):
        objects.disable_tag(conn, bob, tag["tag_id"])
    with pytest.raises(objects.NotFound):  # 他組織には存在を明かさない
        objects.disable_tag(conn, outsider, tag["tag_id"])
    objects.disable_tag(conn, owner, tag["tag_id"])


def test_history_hides_invited_only_cards_from_other_members(conn, alice, bob):
    obj, _ = objects.register_object(conn, alice, "空調")
    c1 = cards.create_card(conn, alice, obj["obj_id"], before_desc="x", scope="org_only")["card"]
    c2 = cards.create_card(conn, alice, obj["obj_id"], before_desc="y", scope="invited_only")["card"]
    assert {c["card_id"] for c in objects.object_history(conn, alice, obj["obj_id"])} == {c1["card_id"], c2["card_id"]}
    assert {c["card_id"] for c in objects.object_history(conn, bob, obj["obj_id"])} == {c1["card_id"]}


def test_history_not_visible_to_other_org(conn, alice, outsider):
    obj, _ = objects.register_object(conn, alice, "空調")
    with pytest.raises(objects.NotFound):
        objects.object_history(conn, outsider, obj["obj_id"])
