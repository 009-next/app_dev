"""Orca 主体・Claude 直は予備。5xx が続く「経路×モデル」を後回しにし、ほかのモデル・経路には影響させない。"""

import pytest

from app import llm
from app.tests.test_timeouts import Boom, ask, route_factory

CFG = {**llm.load_config(), "providers": ["orca", "anthropic"]}


def order(f):
    return [p for p, _ in f.calls]


# ---- Claude 直は、Orca が失敗したときだけ ---------------------------------------------

def test_direct_claude_is_never_called_while_orca_succeeds(conn):
    f = route_factory({})
    for kind in ("decide_light", "decide_heavy", "decide_light"):
        ask(conn, f, kind, CFG)
    assert set(order(f)) == {"orca"}


def test_direct_claude_is_the_fallback_when_orca_fails(conn):
    f = route_factory({"orca": [Boom(500)]})
    r = ask(conn, f, "decide_light", CFG)
    assert r.provider == "anthropic" and "anthropic" in order(f)


# ---- 経路×モデルの後回し ---------------------------------------------------------------

def test_two_server_errors_in_a_row_push_that_model_to_the_back(conn):
    f = route_factory({"orca": [Boom(503)]})
    ask(conn, f, "decide_light", CFG)
    ask(conn, f, "decide_light", CFG)
    f.calls.clear()
    ask(conn, f, "decide_light", CFG)
    assert order(f)[0] == "anthropic"  # 3 回目は、失敗続きの Orca を試す前に、直へ行く


def test_a_single_server_error_does_not_push_anything_back(conn):
    f = route_factory({"orca": [Boom(503), "ok"]})
    ask(conn, f, "decide_light", CFG)
    f.calls.clear()
    ask(conn, f, "decide_light", CFG)
    assert order(f)[0] == "orca"


def test_the_penalty_is_per_model_not_per_route(conn):
    """Orca の Sonnet が 503 でも、Orca の Opus は、先頭のまま試す。"""
    seen = {}

    def factory(provider):
        def create(**kw):
            seen.setdefault(kw["model"], []).append(provider)
            if kw["model"].endswith("claude-sonnet-5") and provider == "orca":
                raise Boom(503)
            from types import SimpleNamespace as N
            usage = N(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
            msg = N(content=[N(type="tool_use", name="no_action", input={"reason": "r", "evidence": ""})], usage=usage)
            return N(headers={"x-orca-resolved-model": kw["model"].split("/")[-1]}, parse=lambda: msg)
        from types import SimpleNamespace as N
        return N(messages=N(with_raw_response=N(create=create)))

    for _ in range(3):
        ask(conn, factory, "decide_light", CFG)
    ask(conn, factory, "decide_heavy", CFG)
    assert seen["anthropic/claude-opus-5"] == ["orca"]  # Opus は、Sonnet の不調に巻き込まれない


def test_the_penalty_expires(conn, monkeypatch):
    f = route_factory({"orca": [Boom(503)]})
    ask(conn, f, "decide_light", CFG)
    ask(conn, f, "decide_light", CFG)
    real = llm.time.time
    monkeypatch.setattr(llm.time, "time", lambda: real() + llm.MODEL_SOFT_COOLDOWN + 1)
    f.calls.clear()
    ask(conn, f, "decide_light", CFG)
    assert order(f)[0] == "orca"  # 期限が切れたら、また Orca から


def test_a_success_clears_the_streak(conn):
    """失敗（1回の呼び出しが、リトライ込みで全滅）→ 成功 → 失敗、は「連続」ではない。"""
    f = route_factory({"orca": [Boom(503), Boom(503), "ok", Boom(503), Boom(503), "ok"]})
    ask(conn, f, "decide_light", CFG)   # 全滅 → 直へ。連続 1
    ask(conn, f, "decide_light", CFG)   # 成功。連続を 0 に戻す
    ask(conn, f, "decide_light", CFG)   # 全滅 → 直へ。連続 1（消していなければ 2 で、後回しになる）
    f.calls.clear()
    ask(conn, f, "decide_light", CFG)
    assert order(f)[0] == "orca"


def test_client_errors_do_not_use_the_model_penalty(conn):
    """4xx（キー・残高・レート制限）は、既存の経路単位の一時停止が担当。ここでは数えない。"""
    f = route_factory({"orca": [Boom(400)]})
    ask(conn, f, "decide_light", CFG)
    assert not llm._model_soft


def test_the_penalty_never_skips_a_route_it_only_reorders(conn):
    """後回しでも、ほかが使えなければ、最後に試す（skip ではない）。"""
    # Orca は 1・2 回目に失敗（リトライ込みで 4 回）。3 回目に直が壊れたら、後回しの Orca に戻って成功する
    f = route_factory({"orca": [Boom(503)] * 4 + ["ok"], "anthropic": ["ok", "ok", Boom(500)]})
    ask(conn, f, "decide_light", CFG)
    ask(conn, f, "decide_light", CFG)
    f.calls.clear()
    r = ask(conn, f, "decide_light", CFG)
    assert order(f)[0] == "anthropic" and "orca" in order(f)
    assert r.provider == "orca"


def test_reset_cooldowns_clears_the_model_penalty(conn):
    f = route_factory({"orca": [Boom(503)]})
    ask(conn, f, "decide_light", CFG)
    ask(conn, f, "decide_light", CFG)
    assert llm._model_soft
    llm.reset_cooldowns()
    assert not llm._model_soft and not llm._model_streak


# ---- 無料モデルの 429 は、そのモデルだけを止める（経路全体・有料モデルを巻き込まない）-------------------

class Free429(Boom):
    """Orca の無料枠の混雑（HTTP 429・code=free_rate_limited）。retry-after は長い（実測で約 15 時間）。"""

    def __init__(self, retry_after="55600"):
        super().__init__(429)
        self.response = type("R", (), {"headers": {"retry-after": retry_after}})()
        self.body = {"error": {"code": "free_rate_limited", "message": "Free model capacity is limited right now."}}


def free_cfg():
    base = llm.load_config()
    return {**base, "providers": ["orca"], "models": {**base["models"], "free_light": "deepseek/deepseek-v4-flash-free"}}


def test_a_free_model_429_does_not_pause_the_whole_route(conn):
    f = route_factory({"orca": [Free429()]})
    with pytest.raises(llm.LLMError):
        ask(conn, f, "free_light", free_cfg())
    assert "orca" not in llm._cooldown            # 経路全体は、止めない
    assert any(k[1].endswith("deepseek-v4-flash-free") for k in llm._model_hard)


def test_a_paid_model_on_the_same_route_is_still_tried_after_a_free_429(conn):
    """本番で起きた欠陥: 無料モデルの 429 が、Orca の Sonnet・Opus まで 5 分止めていた。"""
    cfg = free_cfg()
    f = route_factory({"orca": [Free429()]})
    with pytest.raises(llm.LLMError):
        ask(conn, f, "free_light", cfg)
    ok = route_factory({"orca": ["ok"]})
    r = ask(conn, ok, "decide_light", cfg)  # 同じ経路の、有料の Sonnet
    assert r.provider == "orca"


def test_the_free_model_is_skipped_while_paused_without_calling_it_again(conn):
    cfg = free_cfg()
    f = route_factory({"orca": [Free429()]})
    for _ in range(3):
        with pytest.raises(llm.LLMError):
            ask(conn, f, "free_light", cfg)
    assert len(f.calls) == 1  # 2 回目以降は、混雑している無料モデルを、また叩かない


def test_the_free_pause_is_capped_not_fifteen_hours(conn):
    """retry-after が約 15 時間でも、待つのは COOLDOWN（5 分）まで。使えるようになったら、また試す。"""
    import time
    f = route_factory({"orca": [Free429("55600")]})
    with pytest.raises(llm.LLMError):
        ask(conn, f, "free_light", free_cfg())
    until = next(iter(llm._model_hard.values()))
    assert 0 < until - time.time() <= llm.COOLDOWN


def test_a_paid_model_429_still_pauses_the_route_as_before(conn):
    """既存の動作は変えない（test_routing.py の 429 のテストと同じ）。"""
    f = route_factory({"orca": [Boom(429)]})
    ask(conn, f, "decide_light", CFG)
    assert "orca" in llm._cooldown


def test_free_detection_uses_the_name_or_the_error_code(conn):
    assert llm.is_free_model("deepseek/deepseek-v4-flash-free")
    assert llm.is_free_model("orcarouter/free")
    assert not llm.is_free_model("anthropic/claude-sonnet-5")
    assert not llm.is_free_model("openai/gpt-5-nano")
    assert llm.is_free_error(Free429())
    assert not llm.is_free_error(Boom(429))


def test_reset_clears_the_free_pause(conn):
    f = route_factory({"orca": [Free429()]})
    with pytest.raises(llm.LLMError):
        ask(conn, f, "free_light", free_cfg())
    llm.reset_cooldowns()
    assert not llm._model_hard
