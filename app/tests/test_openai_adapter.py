"""Orca の OpenAI 互換の経路（Claude 以外の小型モデル用）。Anthropic 互換の応答に見せて、llm.call はそのまま使う。"""

import json
from types import SimpleNamespace as N

import pytest

from app import agent, llm
from app.tests.test_timeouts import Boom, ask

NO_ACTION = agent.SPEC["tools"]["no_action"]


@pytest.fixture(autouse=True)
def _priced(monkeypatch):
    """テストでは、gpt-5.4-nano だけ単価を明示する（表にないモデルは、本番と同じくエラーになる）。"""
    monkeypatch.setitem(llm.RATES, "gpt-5-4-nano", (0.2, 1.25))


def openai_client(tool_calls=None, content="", model="gpt-5.4-nano-2026-03-17", finish="tool_calls", usage=(120, 25), raise_=None):
    """chat.completions.with_raw_response.create を持つ偽の OpenAI クライアント。"""
    seen = {}

    def create(**kw):
        seen.update(kw)
        if raise_:
            raise raise_
        msg = N(content=content, tool_calls=tool_calls)
        resp = N(choices=[N(message=msg, finish_reason=finish)], model=model,
                 usage=N(prompt_tokens=usage[0], completion_tokens=usage[1]))
        return N(headers={}, parse=lambda: resp)

    c = N(chat=N(completions=N(with_raw_response=N(create=create))))
    c.seen = seen
    return c


def tc(name="no_action", args=None, raw=None):
    return N(id="call_1", type="function", function=N(name=name, arguments=raw if raw is not None else json.dumps(args or {"reason": "r", "evidence": ""})))


def cfg(**over):
    base = llm.load_config()
    return {**base, "providers": ["orca-openai"], "models": {**base["models"], "small_light": "openai/gpt-5.4-nano"},
            "provider_only": {"small_light": ["orca-openai"]}, **over}


def run(conn, client, config=None):
    return llm.call(conn, "org_1", "small_light", "s", [NO_ACTION], [{"role": "user", "content": "x"}],
                    lambda p: llm.openai_as_anthropic(client), config or cfg())


# ---- 変換 -----------------------------------------------------------------------------

def test_tools_are_sent_as_openai_functions_and_the_system_prompt_becomes_a_message(conn):
    c = openai_client([tc()])
    run(conn, c)
    assert c.seen["tools"][0]["type"] == "function" and c.seen["tools"][0]["function"]["name"] == "no_action"
    assert c.seen["tools"][0]["function"]["parameters"] == NO_ACTION["input_schema"]
    assert c.seen["messages"][0] == {"role": "system", "content": "s"}
    assert c.seen["model"] == "openai/gpt-5.4-nano"  # 非 Claude は、Orca の名前をそのまま送る（anthropic/ を付けない）


def test_a_tool_call_comes_back_as_a_tool_use_block(conn):
    r = run(conn, openai_client([tc(args={"reason": "理由", "evidence": "引用"})]))
    assert r.tool_uses == [{"name": "no_action", "input": {"reason": "理由", "evidence": "引用"}}]


def test_broken_json_arguments_become_an_empty_input_not_a_crash(conn):
    r = run(conn, openai_client([tc(raw="{壊れた")]))
    assert r.tool_uses == [{"name": "no_action", "input": {}}]  # 検証（必須の引数がない）で、既存の仕組みが拒否する


def test_non_object_arguments_are_treated_as_empty(conn):
    r = run(conn, openai_client([tc(raw="[1,2]")]))
    assert r.tool_uses[0]["input"] == {}


def test_a_response_without_tool_calls_has_no_tool_use(conn):
    r = run(conn, openai_client(None, content="ツールを呼ばずに答えました"))
    assert r.tool_uses == [] and "答えました" in r.text


# ---- 原価（表にないモデルは、これまでどおり「未計算」でエラー）---------------------------

def test_an_unpriced_model_is_an_error_not_a_zero(conn):
    with pytest.raises(llm.UncostedModelError):
        run(conn, openai_client([tc()], model="some-model-not-in-the-table"))


def test_a_priced_model_gets_its_cost(conn):
    r = run(conn, openai_client([tc()], model="gpt-5.4-nano-2026-03-17", usage=(1_000_000, 0)))
    assert r.cost_usd == pytest.approx(0.2)


def test_the_call_is_recorded_with_provider_and_resolved_model(conn):
    run(conn, openai_client([tc()]))
    row = conn.execute("SELECT provider, req_model, resolved_model, status FROM llm_call").fetchone()
    assert row["provider"] == "orca-openai" and row["status"] == "ok"
    assert row["req_model"] == "openai/gpt-5.4-nano" and "gpt-5.4-nano" in row["resolved_model"]


# ---- 経路の絞り込み（provider_only）と、失敗時の切り替え -----------------------------------

def test_provider_only_restricts_which_routes_a_kind_may_use(conn):
    """小型モデルの種類は、指定した経路でしか試さない（Claude 直へ、文章を渡さない）。"""
    called = []

    def factory(p):
        called.append(p)
        return llm.openai_as_anthropic(openai_client(raise_=Boom(500))) if p == "orca-openai" else None

    c = cfg(providers=["orca-openai", "anthropic"])
    with pytest.raises(llm.LLMError):
        llm.call(conn, "org_1", "small_light", "s", [NO_ACTION], [{"role": "user", "content": "x"}], factory, c)
    assert called and set(called) == {"orca-openai"}  # anthropic の経路には、一度も行かない


def test_kinds_without_provider_only_use_all_routes(conn):
    """既存の種類は、これまでどおり。"""
    c = {**llm.load_config(), "providers": ["orca", "anthropic"]}
    assert not c.get("provider_only", {}).get("decide_light")


def test_an_openai_error_is_a_normal_route_failure(conn):
    with pytest.raises(llm.LLMError):
        run(conn, openai_client(raise_=Boom(503)))


# ---- 単価の照合: 日付・版の接尾辞だけを許す ------------------------------------------------

@pytest.mark.parametrize("name", ["gpt-5.4-nano", "openai/gpt-5.4-nano-2026-03-17", "gpt-5-4-nano-20260317", "gpt-5.4-nano-latest"])
def test_a_dated_or_versioned_name_matches_the_table(name):
    assert llm.rate_for(name) == (0.2, 1.25)


@pytest.mark.parametrize("name", ["gpt-5.4-nano-pro", "gpt-5.4-nanoX", "gpt-5.4", "gpt-5.4-nano-2026-03-17-ultra", None, ""])
def test_a_different_model_never_borrows_a_similar_price(name):
    """似た名前でも、日付・版でなければ「未計算」（別のモデルの単価で、黙って計算しない）。"""
    assert llm.rate_for(name) is None


def test_the_existing_claude_names_still_resolve():
    assert llm.rate_for("claude-haiku-4-5") == llm.RATES["claude-haiku-4-5"]
    assert llm.rate_for("anthropic/claude-sonnet-5") == llm.RATES["claude-sonnet-5"]
    assert llm.rate_for("claude-haiku-4-5-20251001") == llm.RATES["claude-haiku-4-5"]
