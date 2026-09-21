import pytest

from app import agent, db, llm
from app.tests.fakes import client_failing, client_returning

ORG = "org_1"
INPUTS = {"状況": "エアコンの清掃を実施した"}
COMMON = {"reason": "r", "evidence": "エアコン"}


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    yield c
    c.close()


def _decide(conn, factory, stage="classify", **kw):
    return agent.decide(conn, ORG, stage=stage, inputs=INPUTS, card_id="card_1", client_factory=factory, **kw)


def _logs(conn):
    return db.many(conn, "SELECT * FROM decision_log")


def test_valid_choice_is_applied_and_logged(conn):
    d = _decide(conn, client_returning([("select_card_type", {**COMMON, "type_id": "maintenance"})]))
    assert (d.tool, d.default_used, d.evidence_ok) == ("select_card_type", False, True)
    assert d.args["type_id"] == "maintenance"
    assert len(_logs(conn)) == 1
    assert db.one(conn, "SELECT cost_status FROM llm_call")["cost_status"] == "計算済み"


def test_forbidden_tool_falls_back_to_default(conn):
    d = _decide(conn, client_returning([("publish_share_link", {**COMMON, "scope": "link_30d"})]))
    assert d.default_used and d.boundary_violation
    assert (d.tool, d.args) == agent.DEFAULTS["classify"]
    assert len(_logs(conn)) == 1


def test_schema_violation_falls_back_to_default(conn):
    d = _decide(conn, client_returning([("select_card_type", {**COMMON, "type_id": "not_a_type"})]))
    assert d.default_used
    assert (d.tool, d.args) == agent.DEFAULTS["classify"]
    assert len(_logs(conn)) == 1


def test_widening_scope_is_boundary_violation(conn):
    d = _decide(conn, client_returning([("propose_narrower_scope", {**COMMON, "scope": "link_30d"})]),
                stage="privacy", current_scope="org_only")
    assert d.default_used and d.boundary_violation
    assert d.tool == "no_action"


def test_narrower_scope_is_applied(conn):
    d = _decide(conn, client_returning([("propose_narrower_scope", {**COMMON, "scope": "invited_only"})]),
                stage="privacy", current_scope="org_only")
    assert not d.default_used and d.tool == "propose_narrower_scope"


def test_update_summary_with_unknown_card_id_is_rejected(conn):
    args = {**COMMON, "summary": "s", "evidence_card_ids": ["nope"]}
    d = _decide(conn, client_returning([("update_summary", args)]), stage="summary")
    assert d.default_used and d.tool == "hold_summary"


def test_all_routes_failing_falls_back_and_records_each_attempt(conn):
    d = _decide(conn, client_failing(RuntimeError("boom sk-abcdef123456")))
    assert d.default_used and "LLMの失敗" in d.default_reason
    assert "sk-abcdef123456" not in d.default_reason  # キーは記録しない
    assert len(_logs(conn)) == 1
    assert db.one(conn, "SELECT COUNT(*) c FROM llm_call WHERE status='error'")["c"] == 2  # orca と anthropic


def test_call_limit_reached_uses_default_without_calling_llm(conn):
    ctx = agent.RunCtx(max_calls=0)
    d = _decide(conn, client_failing(AssertionError("呼ばれてはいけない")), ctx=ctx)
    assert d.default_used and "上限" in d.default_reason
    assert db.one(conn, "SELECT COUNT(*) c FROM llm_call")["c"] == 0


def test_uncosted_model_is_an_error_not_zero_cost(conn):
    d = _decide(conn, client_returning([("no_action", COMMON)], resolved_model="unknown-model-x"), stage="periodic")
    assert d.default_used
    row = db.one(conn, "SELECT cost_status, cost_usd FROM llm_call")
    assert (row["cost_status"], row["cost_usd"]) == ("未計算", None)


def test_only_allowed_tools_are_offered(conn):
    seen = {}

    def factory(provider):
        def create(**kw):
            seen["tools"] = [t["name"] for t in kw["tools"]]
            raise RuntimeError("stop")
        return type("C", (), {"messages": type("M", (), {"with_raw_response": type("W", (), {"create": staticmethod(create)})})})()

    _decide(conn, factory)
    assert not set(seen["tools"]) & agent.FORBIDDEN


def test_cost_usd_uses_resolved_model_name():
    assert llm.cost_usd("anthropic/claude-opus-5", 1_000_000, 0) == 5.0
    assert llm.cost_usd("mystery", 10, 10) is None
