"""必要の部屋・組分け帽子・予算の上限・根拠の出どころを、agent.decide に組み込んだ部分。"""

import json

import pytest

from app import agent, db, llm
from app.tests.fakes import client_dynamic, client_returning

C = {"reason": "r", "evidence": ""}
INPUTS = {"作業前の説明": "エアコンのフィルターが汚れている", "作業後の説明": "清掃した"}


def sent_tools(f):
    return [{t["name"] for t in c["tools"]} for c in f.state["calls"]]


ALL = {**llm.load_config(), "room_rules": list(__import__("app.signals", fromlist=["x"]).ALL_RULES)}


def run(conn, stage="classify", tool=("select_card_type", {**C, "type_id": "cleaning"}), **kw):
    f = client_dynamic(lambda kw_, i: [tool])
    dec = agent.decide(conn, "org_1", stage=stage, inputs=INPUTS, client_factory=f, **kw)
    return dec, f


# ---- 必要の部屋 ------------------------------------------------------------------

def test_by_default_every_tool_of_the_stage_is_offered(conn):
    _, f = run(conn)
    assert sent_tools(f)[0] == set(agent.STAGE_TOOLS["classify"])


def test_a_card_without_images_is_not_offered_the_mask_proposal(conn):
    _, f = run(conn, stage="privacy", tool=("no_action", C), has_images=False, config=ALL)
    assert "propose_extra_mask" not in sent_tools(f)[0]
    assert "propose_narrower_scope" in sent_tools(f)[0]


def test_the_narrowest_scope_is_not_offered_a_narrower_one(conn):
    _, f = run(conn, stage="privacy", tool=("no_action", C), current_scope="invited_only")
    assert "propose_narrower_scope" not in sent_tools(f)[0]


def test_a_single_record_is_not_offered_the_contradiction_notice(conn):
    _, f = run(conn, stage="summary", tool=("hold_summary", C), eligible_cards=1, config=ALL)
    assert "draft_notification" not in sent_tools(f)[0]


def test_safety_words_stop_the_summary_from_being_rewritten(conn):
    f = client_dynamic(lambda kw_, i: [("hold_summary", C)])
    dec = agent.decide(conn, "org_1", stage="summary", inputs={"状況": "漏電のおそれがあり、危険です"}, client_factory=f)
    assert dec.risk == "high" and "update_summary" not in sent_tools(f)[0]
    assert "draft_notification" in sent_tools(f)[0]  # 人に知らせる手段は取り上げない


def test_safety_words_start_at_the_stronger_model(conn):
    f = client_dynamic(lambda kw_, i: [("hold_summary", C)], echo_model=True)
    cfg = {**llm.load_config(), "providers": ["anthropic"]}
    dec = agent.decide(conn, "org_1", stage="classify", inputs={"状況": "作業員が感電しかけた"},
                       client_factory=f, config=cfg)
    assert dec.tiers[0]["kind"] == "decide_heavy"  # triage・decide_light を飛ばす


def test_the_reason_for_each_removed_tool_is_written_to_the_decision_log(conn):
    dec, _ = run(conn, stage="privacy", tool=("no_action", C), has_images=False, config=ALL)
    row = db.one(conn, "SELECT room_reasons, risk, validation FROM decision_log WHERE dec_id=?", (dec.dec_id,))
    assert "propose_extra_mask" in row["room_reasons"] and row["risk"] == "low"
    assert "渡さなかったツール" in row["validation"]


def test_narrowing_can_be_switched_off(conn):
    cfg = {**llm.load_config(), "mission_room": False}
    _, f = run(conn, stage="privacy", tool=("no_action", C), has_images=False, config=cfg)
    assert "propose_extra_mask" in sent_tools(f)[0]


def test_a_tool_that_was_not_offered_is_still_refused_if_the_model_calls_it(conn):
    """絞り込みは一次の防御。すり抜けても、従来どおり検証が拒否する。"""
    f = client_returning([("propose_extra_mask", {**C, "target": "顔"})])
    dec = agent.decide(conn, "org_1", stage="privacy", inputs=INPUTS, client_factory=f, has_images=False, config=ALL)
    assert dec.default_used and "propose_extra_mask" not in dec.tool


# ---- 予算の上限 ------------------------------------------------------------------

def test_the_budget_stops_the_switch_to_a_more_expensive_model(conn):
    """裏付けが入力にない応答でも、予算を使い切っていれば上の段へ行かない。"""
    f = client_dynamic(lambda kw_, i: [("select_card_type", {"reason": "r", "evidence": "入力にない文", "type_id": "cleaning"})])
    cfg = {**llm.load_config(), "providers": ["anthropic"]}
    free = agent.decide(conn, "org_1", stage="classify", inputs=INPUTS, client_factory=f, config=cfg)
    assert len(free.tiers) == 3  # 予算がなければ、裏付け不足で最後の段まで上がる

    ctx = agent.RunCtx(budget_usd=0.0003)  # 1回目の呼び出しで使い切る額
    dec = agent.decide(conn, "org_1", stage="classify", inputs=INPUTS, ctx=ctx, client_factory=f, config=cfg)
    assert len(dec.tiers) == 1
    assert "予算の上限" in db.one(conn, "SELECT validation FROM decision_log WHERE dec_id=?", (dec.dec_id,))["validation"]


def test_a_spent_budget_falls_back_to_the_default_without_calling_the_model(conn):
    f = client_dynamic(lambda kw_, i: [("select_card_type", {**C, "type_id": "cleaning"})])
    ctx = agent.RunCtx(budget_usd=0.001)
    ctx.cost = 0.002
    dec = agent.decide(conn, "org_1", stage="classify", inputs=INPUTS, ctx=ctx, client_factory=f)
    assert dec.default_used and "予算の上限" in dec.default_reason and f.state["i"] == 0


def test_no_budget_means_no_limit(conn):
    dec, f = run(conn)
    assert not dec.default_used and f.state["i"] == 1


# ---- 根拠の出どころ（サイコメトリー） ------------------------------------------------

def test_the_decision_records_which_material_the_evidence_came_from(conn):
    sources = [{"field": "カード card_1", "text": "フィルターが汚れている", "source_card_id": "card_1"},
               {"field": "カード card_2", "text": "清掃して風量が回復した", "source_card_id": "card_2"}]
    f = client_returning([("select_card_type", {"reason": "r", "evidence": "清掃して風量が回復した", "type_id": "cleaning"})])
    dec = agent.decide(conn, "org_1", stage="classify", inputs=INPUTS, sources=sources, client_factory=f)
    assert dec.evidence_ok is True
    assert dec.evidence_source == {"field": "カード card_2", "card_id": "card_2"}
    assert json.loads(db.one(conn, "SELECT evidence_source FROM decision_log WHERE dec_id=?",
                             (dec.dec_id,))["evidence_source"])["card_id"] == "card_2"


def test_evidence_that_is_in_no_single_material_has_no_source(conn):
    sources = [{"field": "a", "text": "フィルターが汚れている", "source_card_id": "card_1"},
               {"field": "b", "text": "清掃した", "source_card_id": "card_2"}]
    f = client_returning([("select_card_type", {"reason": "r", "evidence": "存在しない文です", "type_id": "cleaning"})])
    dec = agent.decide(conn, "org_1", stage="classify", inputs=INPUTS, sources=sources, client_factory=f)
    assert dec.evidence_ok is False and dec.evidence_source is None


@pytest.mark.parametrize("evidence", ["", "   "])
def test_no_evidence_means_no_source(conn, evidence):
    f = client_returning([("no_action", {"reason": "r", "evidence": evidence})])
    dec = agent.decide(conn, "org_1", stage="privacy", inputs=INPUTS, client_factory=f)
    assert dec.evidence_source is None
