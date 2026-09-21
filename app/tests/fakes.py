"""テスト用の偽 LLM クライアント。実 API は呼ばない。"""

from __future__ import annotations

from types import SimpleNamespace


def _msg(tool_uses: list[tuple[str, dict]], in_tok=100, out_tok=20):
    content = [SimpleNamespace(type="tool_use", name=n, input=i) for n, i in tool_uses]
    usage = SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok,
                            cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(content=content, usage=usage)


def client_returning(tool_uses: list[tuple[str, dict]], resolved_model: str = "claude-sonnet-5"):
    """指定したツール呼び出しを返すクライアントの factory。"""
    raw = SimpleNamespace(headers={"x-orca-resolved-model": resolved_model}, parse=lambda: _msg(tool_uses))
    create = lambda **kw: raw  # noqa: E731
    client = SimpleNamespace(messages=SimpleNamespace(with_raw_response=SimpleNamespace(create=create)))
    return lambda provider: client


def client_failing(exc: Exception):
    """常に例外を送出するクライアントの factory。"""
    def create(**kw):
        raise exc
    client = SimpleNamespace(messages=SimpleNamespace(with_raw_response=SimpleNamespace(create=create)))
    return lambda provider: client


def client_sequence(responses: list[list[tuple[str, dict]]], resolved_model: str = "claude-sonnet-5"):
    """呼び出しごとに、順に別の応答を返す factory。使い切ったら最後の応答を繰り返す。呼び出し回数は .calls。"""
    state = {"i": 0}

    def create(**kw):
        tu = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        return SimpleNamespace(headers={"x-orca-resolved-model": resolved_model}, parse=lambda: _msg(tu))

    client = SimpleNamespace(messages=SimpleNamespace(with_raw_response=SimpleNamespace(create=create)))
    factory = lambda provider: client  # noqa: E731
    factory.state = state
    return factory


def client_dynamic(fn, resolved_model: str = "claude-sonnet-5", echo_model: bool = False):
    """fn(呼び出しの引数, 何回目か) -> [(ツール名, 引数)] を返す factory。送ったプロンプト（カードIDなど）を見て応答を作る。"""
    state = {"i": 0, "calls": []}

    def create(**kw):
        state["calls"].append(kw)
        tu = fn(kw, state["i"])
        state["i"] += 1
        hdr = kw["model"].split("/")[-1] if echo_model else resolved_model  # echo_model: 依頼したモデル名をそのまま返す（原価の確認用）
        return SimpleNamespace(headers={"x-orca-resolved-model": hdr}, parse=lambda: _msg(tu))

    client = SimpleNamespace(messages=SimpleNamespace(with_raw_response=SimpleNamespace(create=create)))
    factory = lambda provider: client  # noqa: E731
    factory.state = state
    return factory
