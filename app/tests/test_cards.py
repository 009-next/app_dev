import os

import pytest

from app import agent, authz, cards, db, images, objects, sharing
from app.tests.fakes import client_failing, client_sequence

COMMON = {"reason": "r", "evidence": "エアコン"}
WRITE = ("write_card_text", {"title": "エアコン清掃", "changes": ["フィルターを清掃"], "description": "清掃した"})


@pytest.fixture
def obj(conn, alice):
    return objects.register_object(conn, alice, "空調")[0]


def _agent_ok():
    return client_sequence([
        [("select_card_type", {**COMMON, "type_id": "cleaning"})],
        [("no_action", COMMON)],
        [WRITE],
        [("hold_summary", COMMON)],  # 要約の段階。無効な応答（WRITE の繰り返し）だと、上位のモデルへ切り替えるので、有効な応答を用意する
    ])


def test_create_card_runs_agent_and_saves_results(conn, alice, obj, jpeg, tmp_path):
    f = _agent_ok()
    r = cards.create_card(conn, alice, obj["obj_id"], before_desc="エアコンが汚れている", after_desc="きれい",
                          images_in=[("before", jpeg, None), ("after", jpeg, [[0, 0, 0.5, 0.5]])],
                          client_factory=f, data_dir=tmp_path)
    card = r["card"]
    assert (card["card_type"], card["scope"], card["title"]) == ("cleaning", "link_30d", "エアコン清掃")
    assert f.state["i"] == 4  # classify / privacy / describe / summary
    assert len(r["decisions"]) == 3 and r["cost_usd"] > 0
    assert db.one(conn, "SELECT COUNT(*) c FROM decision_log")["c"] == 3
    files = os.listdir(tmp_path)
    assert len(files) == 2
    assert not images.has_metadata((tmp_path / files[0]).read_bytes())


def test_no_material_means_no_llm_call(conn, alice, obj):
    f = client_failing(AssertionError("呼ばれてはいけない"))
    r = cards.create_card(conn, alice, obj["obj_id"], client_factory=f)
    assert r["decisions"] == [] and r["card"]["card_type"] == "generic"
    assert db.one(conn, "SELECT COUNT(*) c FROM llm_call")["c"] == 0


def test_llm_failure_falls_back_and_card_is_still_created(conn, alice, obj):
    r = cards.create_card(conn, alice, obj["obj_id"], before_desc="x", client_factory=client_failing(RuntimeError("down")))
    assert r["card"]["card_type"] == "generic" and r["card"]["ai_text"] is None
    assert all(d.default_used for d in r["decisions"])


def test_bad_image_creates_nothing(conn, alice, obj, tmp_path):
    with pytest.raises(images.ImageError):
        cards.create_card(conn, alice, obj["obj_id"], before_desc="x", images_in=[("before", b"junk", None)],
                          data_dir=tmp_path)
    assert db.one(conn, "SELECT COUNT(*) c FROM card")["c"] == 0 and os.listdir(tmp_path) == []


def test_ai_scope_proposal_is_saved_but_not_applied(conn, alice, obj):
    f = client_sequence([[("no_action", COMMON)],
                         [("propose_narrower_scope", {**COMMON, "scope": "org_only"})], [WRITE]])
    card = cards.create_card(conn, alice, obj["obj_id"], before_desc="エアコン", client_factory=f)["card"]
    assert (card["scope"], card["proposed_scope"]) == ("link_30d", "org_only")
    cards.apply_proposed_scope(conn, alice, card["card_id"])
    after = cards.get_card(conn, alice, card["card_id"])
    assert (after["scope"], after["proposed_scope"]) == ("org_only", None)


def test_ai_widening_proposal_is_rejected_by_guard(conn, alice, obj):
    f = client_sequence([[("no_action", COMMON)], [("propose_narrower_scope", {**COMMON, "scope": "link_30d"})], [WRITE]])
    card = cards.create_card(conn, alice, obj["obj_id"], before_desc="エアコン", scope="org_only", client_factory=f)["card"]
    assert card["proposed_scope"] is None


def test_outsider_cannot_create_or_see(conn, alice, obj, outsider):
    with pytest.raises(objects.NotFound):
        cards.create_card(conn, outsider, obj["obj_id"], before_desc="x")
    c = cards.create_card(conn, alice, obj["obj_id"], scope="org_only")["card"]
    with pytest.raises(objects.NotFound):
        cards.get_card(conn, outsider, c["card_id"])


def test_get_card_hides_invited_only_from_other_member(conn, alice, bob, owner, obj):
    c = cards.create_card(conn, alice, obj["obj_id"], scope="invited_only")["card"]
    assert cards.get_card(conn, alice, c["card_id"]) and cards.get_card(conn, owner, c["card_id"])
    with pytest.raises(objects.NotFound):
        cards.get_card(conn, bob, c["card_id"])


def test_narrow_scope_rules(conn, alice, bob, obj):
    c = cards.create_card(conn, alice, obj["obj_id"], scope="org_only")["card"]
    with pytest.raises(ValueError):
        cards.narrow_scope(conn, alice, c["card_id"], "link_30d")
    with pytest.raises(objects.Denied):
        cards.narrow_scope(conn, bob, c["card_id"], "invited_only")
    cards.narrow_scope(conn, alice, c["card_id"], "invited_only")


def test_widen_needs_owner_member_must_request(conn, alice, owner, obj):
    c = cards.create_card(conn, alice, obj["obj_id"], scope="invited_only")["card"]
    with pytest.raises(objects.Denied):
        cards.widen_scope(conn, alice, c["card_id"], "org_only")
    ap = cards.request_widen_scope(conn, alice, c["card_id"], "org_only")
    assert cards.get_card(conn, alice, c["card_id"])["scope"] == "invited_only"  # 申請だけでは変わらない
    with pytest.raises(objects.Denied):
        cards.decide_widen_request(conn, alice, ap, True)
    cards.decide_widen_request(conn, owner, ap, True)
    assert cards.get_card(conn, alice, c["card_id"])["scope"] == "org_only"


def test_rejected_widen_request_changes_nothing(conn, alice, owner, obj):
    c = cards.create_card(conn, alice, obj["obj_id"], scope="invited_only")["card"]
    ap = cards.request_widen_scope(conn, alice, c["card_id"], "link_30d")
    cards.decide_widen_request(conn, owner, ap, False)
    assert cards.get_card(conn, alice, c["card_id"])["scope"] == "invited_only"
    with pytest.raises(objects.NotFound):  # 決定済みの申請は再決定できない
        cards.decide_widen_request(conn, owner, ap, True)


def test_delete_card_only_creator_or_owner(conn, alice, bob, owner, obj):
    c = cards.create_card(conn, alice, obj["obj_id"], scope="org_only")["card"]
    with pytest.raises(objects.Denied):
        cards.delete_card(conn, bob, c["card_id"])
    cards.delete_card(conn, alice, c["card_id"])
    with pytest.raises(objects.NotFound):
        cards.get_card(conn, owner, c["card_id"])


def test_issue_share_requires_confirmation_scope_and_masks(conn, alice, bob, obj, jpeg, tmp_path):
    c = cards.create_card(conn, alice, obj["obj_id"], images_in=[("before", jpeg, None)], data_dir=tmp_path)["card"]
    with pytest.raises(cards.NotReady):
        cards.issue_share(conn, alice, c["card_id"], confirmed=False)
    with pytest.raises(cards.NotReady):  # ぼかしの確認が済んでいない
        cards.issue_share(conn, alice, c["card_id"], confirmed=True)
    db.run(conn, "UPDATE image SET mask_confirmed=1")
    with pytest.raises(cards.NotReady):  # ぼかしが済んでいても、確認画面の承認がなければ発行しない
        cards.issue_share(conn, alice, c["card_id"], confirmed=False)
    with pytest.raises(objects.Denied):
        cards.issue_share(conn, bob, c["card_id"], confirmed=True)
    s = cards.issue_share(conn, alice, c["card_id"], confirmed=True)
    assert s["token"].startswith("s_") and s["approver_id"] == alice.member_id
    cards.narrow_scope(conn, alice, c["card_id"], "org_only")
    assert sharing.resolve_share(conn, s["token"]) is None  # 範囲を狭めたらリンクは働かない
    with pytest.raises(cards.NotReady):
        cards.issue_share(conn, alice, c["card_id"], confirmed=True)


def test_agent_tools_never_include_share_widen_or_delete():
    offered = {t for tools in agent.STAGE_TOOLS.values() for t in tools}
    assert not offered & {"publish_share_link", "widen_scope", "remove_mask", "delete_card", "send_notification"}
    assert not authz.can(authz.Actor(kind="ai", org_id="org_1"), "issue_share", {"org_id": "org_1"})
