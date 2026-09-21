import pytest

from app import authz

ORG = "org_1"
ACTORS = {
    "owner": authz.Actor(kind="member", org_id=ORG, member_id="m1", role="owner"),
    "member": authz.Actor(kind="member", org_id=ORG, member_id="m2", role="member"),
    "invited": authz.Actor(kind="invited", invite_id="i1", card_id="c1"),
    "link": authz.Actor(kind="link", card_id="c1"),
    "anon": authz.ANON,
    "ai": authz.Actor(kind="ai", org_id=ORG),
}


@pytest.mark.parametrize("action,role", authz.denied_cells())
def test_denied_cells_are_denied(action, role):
    resource = {"org_id": ORG, "card_id": "c1", "creator_id": "m2", "registrant_id": "m2", "issuer_id": "m2"}
    assert authz.can(ACTORS[role], action, resource) is False


def test_ai_cannot_widen_or_issue_share():
    assert not authz.can(ACTORS["ai"], "widen_scope", {"org_id": ORG})
    assert not authz.can(ACTORS["ai"], "issue_share", {"org_id": ORG})
    assert not authz.can(ACTORS["ai"], "send_notification", {"org_id": ORG})


def test_other_org_member_is_anon():
    other = authz.Actor(kind="member", org_id="org_2", member_id="x", role="owner")
    assert not authz.can(other, "view_card", {"org_id": ORG, "scope": "org_only"})


def test_periodic_search_only_when_flagged():
    res = {"org_id": ORG}
    assert not authz.can(ACTORS["ai"], "search_org_records", res)
    assert authz.can(authz.Actor(kind="ai", org_id=ORG, periodic=True), "search_org_records", res)


def test_scope_is_narrower():
    assert authz.scope_is_narrower("org_only", "link_30d")
    assert not authz.scope_is_narrower("link_30d", "org_only")
    assert not authz.scope_is_narrower("org_only", "org_only")
    assert not authz.scope_is_narrower("public", "link_30d")
