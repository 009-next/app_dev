"""説明文の段階切り替え（routing.describe）。既定はオフで、従来どおり Sonnet を 1 回。"""

from app import agent, llm
from app.tests.fakes import client_dynamic, client_failing

INPUTS = {"作業前の説明": "エアコン RA-25 のフィルターが汚れている", "作業後の説明": "清掃した。風量が回復した"}


def w(title="エアコン清掃", desc="RA-25 のフィルターを清掃して風量が回復した"):
    return [("write_card_text", {"title": title, "changes": ["清掃した"], "description": desc})]


def cfg(enabled=True, tiers=("triage", "describe")):
    return {**llm.load_config(), "providers": ["anthropic"],
            "routing": {**llm.load_config()["routing"], "describe": {"enabled": enabled, "tiers": list(tiers)}}}


def run(conn, factory, config):
    ctx = agent.RunCtx()
    return agent.describe(conn, "org_1", INPUTS, ctx, factory, config), ctx


def models(f):
    return [c["model"] for c in f.state["calls"]]


def test_default_is_off_and_calls_the_describe_model_once(conn):
    f = client_dynamic(lambda kw, i: w())
    out, _ = run(conn, f, {**llm.load_config(), "providers": ["anthropic"]})
    assert out and models(f) == ["claude-sonnet-5"]


def test_enabled_uses_the_cheap_model_first_and_stops_when_grounded(conn):
    f = client_dynamic(lambda kw, i: w())
    out, ctx = run(conn, f, cfg())
    assert out and models(f) == ["claude-haiku-4-5"] and ctx.llm_calls == 0


def test_ungrounded_number_or_model_code_escalates_to_the_next_model(conn):
    f = client_dynamic(lambda kw, i: w(desc="RA-30 を 3 台清掃した") if i == 0 else w())
    out, _ = run(conn, f, cfg())
    assert models(f) == ["claude-haiku-4-5", "claude-sonnet-5"] and "RA-25" in out["description"]


def test_last_tier_is_accepted_even_if_ungrounded(conn):
    f = client_dynamic(lambda kw, i: w(desc="99 台清掃した"))
    out, _ = run(conn, f, cfg())
    assert out and len(models(f)) == 2  # 従来（Sonnet 1 回）と同じ扱い。最後のモデルの出力は、そのまま採用する


def test_invalid_response_from_the_cheap_model_escalates(conn):
    f = client_dynamic(lambda kw, i: [("no_action", {"reason": "r", "evidence": ""})] if i == 0 else w())
    out, _ = run(conn, f, cfg())
    assert out and len(models(f)) == 2


def test_ungrounded_answer_is_kept_if_the_next_model_fails(conn):
    seq = [w(desc="99 台清掃した"), [("no_action", {"reason": "r", "evidence": ""})]]
    f = client_dynamic(lambda kw, i: seq[i])
    out, _ = run(conn, f, cfg())
    assert out and "99" in out["description"]


def test_error_on_all_tiers_returns_none_like_before(conn):
    out, _ = run(conn, client_failing(RuntimeError("x")), cfg())
    assert out is None


def test_high_quality_ignores_the_switch(conn):
    f = client_dynamic(lambda kw, i: w())
    ctx = agent.RunCtx()
    agent.describe(conn, "org_1", INPUTS, ctx, f, cfg(), quality="high")
    assert models(f) == ["claude-opus-5"]


def test_llm_call_limit_stops_escalation(conn):
    f = client_dynamic(lambda kw, i: w(desc="99 台"))
    ctx = agent.RunCtx()
    ctx.llm_calls = ctx.max_llm_calls
    agent.describe(conn, "org_1", INPUTS, ctx, f, cfg())
    assert len(models(f)) == 1


def test_ungrounded_facts_uses_normalized_text():
    out = {"title": "清掃", "changes": ["３台 を清掃"], "description": "型番 ra-25 を点検。2026.9 に実施"}
    assert agent.ungrounded_facts(out, {"x": "3台。RA-25。"}) == ["2026.9"]
