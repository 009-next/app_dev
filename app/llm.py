"""LLM 呼び出し層: Orca Router を主、失敗したら Claude API（Anthropic 直）へ Fallback。

- Fallback は 5xx・429・通信障害だけでなく、401 などの 4xx でも働く（Orca 側の fallback は 4xx で動かない可能性があるため）。
- すべての呼び出し（失敗を含む）を llm_call に記録する。原価は、応答で解決されたモデル名で計算する。
  単価表にないモデルは、0円としてでなく「未計算」として記録し、エラーにする。
- API キーは環境変数だけ。ファイル・ログ・応答に出さない。
- 経路の優先は config["providers"] の順（既定は Orca → Claude API 直）。キー・残高・権限（401/402/403）や
  レート制限（429）で失敗した経路は、しばらく（COOLDOWN 秒）使わない。使える経路だけを、毎回試す。
- 呼び出しごとに、種類（kind）別のタイムアウト（config["timeouts"]）を渡す。SDK の自動リトライは切り、
  タイムアウトは「遅い」として次の経路へ、一時的な故障（5xx・通信）は同じ経路で1回だけやり直す（config["retry_transient"]）。
  連続2回タイムアウトした経路は、60秒だけ後回しにする（使えない経路が1つしかなければ、後回しでも試す）。
  すべての経路がタイムアウトなら LLMTimeout（LLMError の一種）。agent.decide は、これを見て次のモデルの段へ進む。
- 判断の種類（kind）ごとのモデルは config["models"]。agent.decide は、段階ごとに安い順（triage=Haiku → light=Sonnet → heavy=Opus）へ切り替える。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import time
from dataclasses import dataclass, field

from . import db

ORCA_BASE_URL = "https://api.orcarouter.ai"

DEFAULT_CONFIG = {
    "providers": ["orca", "anthropic"],  # 先頭が主、以降が Fallback
    "models": {  # 判断の種類ごとのモデル（Anthropic の名前。Orca では anthropic/ を付ける）
        "describe": "claude-sonnet-5",
        "describe_refine": "claude-opus-5",  # 説明文に細かい修正が必要なとき（agent.describe(quality="high")）
        "triage": "claude-haiku-4-5",       # 状況の一次分析（最も安い）
        "decide_light": "claude-sonnet-5",
        "decide_heavy": "claude-opus-5",
        # Claude 以外の小型モデル（Orca 経由）。分類だけ・条件を満たすときだけ先に試す（agent.decide → signals.external_ok）。
        # 2026-09-21 の実測（run_small_probe.py・14 モデル）で、採用基準を満たしたのは deepseek-v4.1-flash だけ:
        # 分類 14/15・応答の中央値 3.6 秒・入力 $0.15/出力 $0.60。無料モデルは、一致率 20〜67%・429 が多く、不合格（切り替え表に入れない）
        "small_light": "deepseek/deepseek-v4.1-flash",
        "free_light": "deepseek/deepseek-v4-flash-free",
        # 音声分析（音声を音声対応モデルへ渡し、文字と発話の構造を得る。第2の経路・既定オフ）。Orca で音声入力に対応するのは Gemini 系・Meta の Muse Spark だけ
        # （Claude・OpenAI・Qwen・GLM にはない。2026-09-21 の /v1/models）。安い順の第1〜3。音声は Google 等の第三者へ渡る。
        # 2026-09-21 の実測（合成音声 10 本・run_audio_compare.py）: 文字の類似度 gemini-2.5-flash-lite 0.81／3.5-flash-lite 0.97（$0.0003/本・3.9 秒）／
        # 3.5-flash 0.97（$0.004）／3.1-pro-preview 0.98（$0.010・16 秒）。安くて十分な 3.5-flash-lite を第1にした
        "audio_light": "google/gemini-3.5-flash-lite",
        "audio_mid": "google/gemini-3.5-flash",
        "audio_heavy": "google/gemini-3.1-pro-preview",
    },
    # Orca 側のモデル名が Anthropic の名前と違うもの
    "orca_model_names": {"claude-haiku-4-5": "claude-haiku-4.5"},
    # 段階ごとに、安い順に試す。裏付けが入力にない・形式が壊れた・機微な語があるのに何もしない、などのときだけ次の段へ切り替える。
    # 2026-09-20 の実測（x10 の9場面）: 分類系は Haiku が 20/20、共有（機微な内容）と定期の矛盾検出は Haiku では不足。
    "routing": {
        "enabled": True,
        "stages": {
            "classify": ["triage", "decide_light", "decide_heavy"],
            "privacy": ["triage", "decide_light", "decide_heavy"],
            "summary": ["decide_light", "decide_heavy"],
            "periodic": ["decide_light", "decide_heavy"],
            "options": ["decide_light", "decide_heavy"],
        },
        # 説明文（write_card_text）を、安いモデルから試す。既定はオフ（品質評価を通ってから、承認を得て有効にする）
        "describe": {"enabled": False, "tiers": ["triage", "describe"]},
    },
    # 種類ごとに、使ってよい経路を絞る。小型・無料モデルの文章を、ほかの経路（Claude 直）へ回さないため。既定は空（絞らない）。
    "provider_only": {"small_light": ["orca"], "free_light": ["orca"],
                      "audio_light": ["orca-openai"], "audio_mid": ["orca-openai"], "audio_heavy": ["orca-openai"]},  # Claude 以外の小型モデルの文章を、Claude 直へ回さない
    "external_kinds": ["small_light", "free_light", "audio_light", "audio_mid", "audio_heavy"],  # 第三者の提供元へ文章が渡る種類。signals.external_ok が通らなければ、飛ばす
    "max_tokens": 2048,
    "timeout": 60.0,  # kind が config["timeouts"] にないときの、1呼び出しの上限（秒）
    # 実測の平均（Haiku 3.7 秒・Sonnet 6 秒・Opus 12 秒）の約3倍。Haiku で 34.5 秒かかった外れ値が1回あった（2026-09-20）
    "timeouts": {"triage": 12.0, "describe": 20.0, "describe_refine": 45.0, "decide_light": 25.0, "decide_heavy": 45.0,
                 "audio_light": 45.0, "audio_mid": 60.0, "audio_heavy": 90.0},
    "retry_transient": 1,  # 5xx・通信の故障を、同じ経路でやり直す回数（タイムアウトは、やり直さず次の経路へ）
    "job_deadline": 120.0,  # カード作成の AI の処理全体の期限（秒。cards の並列の待ちに使う）
    # 応答の深さ（思考）の設定。既定は空（従来どおり＝モデルの既定）。実測して、判断の一致率を保てた種類だけに設定する。
    # effort: {"decide_light": "low"} のように、種類ごと。対応するモデル（EFFORT_MODELS）と、Claude API 直の経路だけに送る。
    # thinking_off: ["decide_light"] のように、思考を切る種類（THINKING_OFF_MODELS だけ）。
    "effort_by_kind": {"decide_light": "low", "decide_heavy": "low"},  # 9場面×3回で一致率 27/27（既定と同じ）・応答 6.3→4.6 秒を確認して、既定にした（2026-09-20）
    "thinking_off": [],
    # 必要の部屋（signals.room）で使う絞り込みの規則。既定は、_guard がすでに拒否しているものと、安全に関わるものだけ。
    # "images"（画像のないカードにぼかしの提案を渡さない）と "records"（根拠1件で矛盾の通知を渡さない）は、
    # 判断の内容を変えるので、実 API で一致率を確かめてから既定に入れる。
    "room_rules": ["scope", "question", "hazard"],
    # 会話から次の業務を考える機能（app/talk_loop.py）。"single"=会話・画面・記録を全部渡して 1 回で提案／"loop"=記録・画面を AI が必要なときだけ読んで調べ直す。
    # 実測（10 場面・各 1 回）: 初回の合格は単発 9/10・ループ 8/10、1 件の費用は $0.0098 と $0.0163。ループは、同意率を上げず、費用が約 1.7 倍だったので、既定は単発にした。
    # 作り手の「違う」→ 修正 → 再提案の外側のループは、どちらの方式でも動く。
    "talk_mode": "single",
    "talk_tiers": ["decide_light", "decide_heavy"],
    "talk_audio_escalate": True,   # 聞き取りにくい音声（unclear、または長さに対して文字が極端に少ない）は、次の段へ上げる（A26）。実測（合成音声 60 実行）では、雑音 0dB でも上がらず、類似度は 0.76→0.76 で効果は確認できなかった。費用は増えない
}

# API の仕様（claude-api スキル、2026-06 時点）: effort は Sonnet 5・Opus 5 など。Haiku 4.5 は 400。思考を切れるのは、Opus 5 では
# 「ツール呼び出しが本文に書かれて実行されない」不具合の恐れがあるので、Sonnet 5 だけに限る。
EFFORT_MODELS = {"claude-sonnet-5", "claude-opus-5"}
THINKING_OFF_MODELS = {"claude-sonnet-5"}

# 単価（USD / 100万トークン）。表にないモデルは「未計算」。
RATES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # Orca の公開価格表（/api/pricing。入力 = model_ratio × 2、出力 = 入力 × completion_ratio）で確認したものだけ。
    # Claude 3 モデルで、この式がこの表と一致することを確認済み（2026-09-21）
    "deepseek-v4-1-flash": (0.15, 0.60),
    # 音声分析の Gemini（2026-09-21 の /api/pricing）。音声の入力トークンの単価は、公開価格表では区別されない（推定）。
    "gemini-2-5-flash-lite": (0.10, 0.40),
    "gemini-3-5-flash-lite": (0.30, 2.50),
    "gemini-3-5-flash": (1.50, 9.00),
    "gemini-3-1-pro-preview": (2.0, 12.0),
}


class LLMError(Exception):
    """すべての経路が失敗した。"""


class LLMTimeout(LLMError):
    """試した経路が、タイムアウトだけで失敗した（故障ではなく、遅い）。上のモデルの段へ進む価値がある。"""


class UncostedModelError(LLMError):
    """単価表にないモデルで応答が返った（原価を計算できない）。"""


# 実行時のプロファイル（環境変数 MIRUCON_LLM_PROFILE）。既定（未設定）は、従来の切り替え表（Haiku が最初）のまま。
# "orca": Orca 主体・Haiku なし（2026-09-21 の指示）。製品の起動（web.main）と検証スクリプトが使う。
#   - Orca の Haiku 4.5 が 503 を返し続けたため、どの段にも入れない
#   - 分類だけが、Claude 以外の小型モデル（small_light）を先に試す（signals.external_ok の条件を満たすときだけ）
#   - 共有・要約・定期は、Orca の Sonnet から
# 既定を変えないのは、従来の「上のモデルへ進む」仕組みを守るテストを、そのまま残すため（違いは切り替え表だけ）。
PROFILES = {
    "orca": {"routing_stages": {"classify": ["small_light", "decide_light", "decide_heavy"],
                                "privacy": ["decide_light", "decide_heavy"]}},
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    prof = PROFILES.get(os.environ.get("MIRUCON_LLM_PROFILE", ""))
    if prof:
        cfg["routing"]["stages"].update(json.loads(json.dumps(prof["routing_stages"])))
    path = os.environ.get("MIRUCON_LLM_CONFIG") or str(pathlib.Path(__file__).with_name("llm_config.json"))
    if os.path.exists(path):
        cfg.update(json.loads(pathlib.Path(path).read_text(encoding="utf-8")))
    return cfg


COOLDOWN = 300.0  # 秒。キー・残高・権限・レート制限で失敗した経路を、この間は試さない
COOLDOWN_STATUSES = {401, 402, 403, 429}
_cooldown: dict[str, float] = {}
SOFT_COOLDOWN = 60.0  # 秒。連続 TIMEOUT_STREAK 回タイムアウトした経路を、この間は後回しにする（試さないのではない）
TIMEOUT_STREAK = 2
_soft_cooldown: dict[str, float] = {}
_timeout_streak: dict[str, int] = {}
# 5xx が続く「経路×モデル」を後回しにする。Orca の Haiku が 503 を返し続けても、Orca の Sonnet・Opus は巻き込まない。
# 無料モデルの 429（混雑）は、そのモデルだけを止める。無料枠の容量はモデルごとの事情で、経路全体の故障ではない。
# 経路ごと止めると、同じ経路の有料モデル（Sonnet・Opus）まで巻き込まれる（実測で発生）。
_model_hard: dict[tuple[str, str], float] = {}
MODEL_SOFT_COOLDOWN = 120.0  # 秒
MODEL_STREAK = 2
_model_soft: dict[tuple[str, str], float] = {}
_model_streak: dict[tuple[str, str], int] = {}
TIMEOUT_NAMES = {"APITimeoutError", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException", "TimeoutError"}


def reset_cooldowns() -> None:
    _cooldown.clear()
    _soft_cooldown.clear()
    _timeout_streak.clear()
    _model_soft.clear()
    _model_streak.clear()
    _model_hard.clear()


def is_timeout(e: BaseException) -> bool:
    """SDK・httpx・標準のタイムアウトを、クラス名で見分ける（ここで anthropic / httpx を読み込まない）。"""
    return any(c.__name__ in TIMEOUT_NAMES for c in type(e).__mro__)


def third_party(model: str | None) -> str:
    """文章が渡った提供元（モデル名の前半。deepseek・qwen・openai など）。Claude（anthropic）は、Orca の窓口でも Anthropic のモデル。"""
    m = (model or "").lower()
    if "/" in m:
        return m.split("/")[0]
    for prefix in ("deepseek", "qwen", "gpt", "gemini", "glm", "kimi", "minimax", "hy3"):
        if m.startswith(prefix):
            return {"gpt": "openai", "gemini": "google", "glm": "z-ai", "hy3": "tencent"}.get(prefix, prefix)
    return "anthropic" if m.startswith("claude") else (m or "不明")


def is_free_model(name: str | None) -> bool:
    n = (name or "").lower()
    return n.endswith("-free") or n.endswith("/free")


def is_free_error(e: BaseException) -> bool:
    """Orca の無料枠の混雑（429・code=free_rate_limited）。"""
    body = getattr(e, "body", None)
    code = ((body or {}).get("error") or {}).get("code") if isinstance(body, dict) else None
    return getattr(e, "status_code", None) == 429 and code == "free_rate_limited"


def _retry_after(e: BaseException) -> float:
    try:
        return min(COOLDOWN, float(getattr(getattr(e, "response", None), "headers", {}).get("retry-after", COOLDOWN)))
    except (TypeError, ValueError):
        return COOLDOWN


def is_transient(e: BaseException) -> bool:
    """同じ経路でやり直す価値のある故障: 5xx・過負荷・通信の失敗（タイムアウトは除く）。"""
    status = getattr(e, "status_code", None)
    if status is not None:
        return status >= 500
    return any(c.__name__ in ("APIConnectionError", "ConnectError", "RemoteProtocolError", "ReadError") for c in type(e).__mro__)


def _cool_down(provider: str, e: Exception) -> None:
    status = getattr(e, "status_code", None)
    if status in COOLDOWN_STATUSES:  # キー未設定（LLMError）は、通信なしで確かめられるので、一時停止にしない
        wait = COOLDOWN
        if status == 429:
            try:
                wait = min(COOLDOWN, float(getattr(getattr(e, "response", None), "headers", {}).get("retry-after", COOLDOWN)))
            except (TypeError, ValueError):
                pass
        _cooldown[provider] = time.time() + wait


def norm_model(name: str | None) -> str:
    return (name or "").lower().split("/")[-1].replace(".", "-")


# 応答で解決された名前の、日付・版の接尾辞（gpt-5-4-nano-2026-03-17、…-20251001、…-ga-260731）。これだけを、表の名前の後ろに許す。
# 任意の接頭辞一致にはしない（表にないモデルを、似た名前の単価で計算してしまわないため）。
_VERSION_SUFFIX = re.compile(r"^(?:-\d{4}-\d{2}-\d{2}|-\d{6,8}|-ga-\d{6}|-latest)+$")


def rate_for(model: str | None):
    name = norm_model(model)
    if name in RATES:
        return RATES[name]
    for key in sorted(RATES, key=len, reverse=True):
        if name.startswith(key) and _VERSION_SUFFIX.match(name[len(key):]):
            return RATES[key]
    return None


def cost_usd(model: str | None, in_tok: int, out_tok: int, cache_read: int = 0, cache_write: int = 0):
    rate = rate_for(model)
    if rate is None:
        return None
    i, o = rate
    return (in_tok * i + out_tok * o + cache_read * i * 0.1 + cache_write * i * 1.25) / 1_000_000


_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+\S+)")


def redact(text: str, limit: int = 300) -> str:
    return _SECRET.sub("[REDACTED]", str(text))[:limit]


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def openai_as_anthropic(client):
    """OpenAI 互換のクライアントを、llm.call が期待する形（messages.with_raw_response.create → .headers / .parse()）に見せる。
    ツールは function に、tool_calls は tool_use ブロックに変換する。壊れた JSON の引数は、空の入力にする
    （必須の引数がない、として、既存の検証が拒否する）。"""

    def create(*, model, max_tokens, system, tools, messages, timeout=None, **_ignored):
        oa_tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                      "parameters": t["input_schema"]}} for t in tools]
        kw = {"model": model, "max_tokens": max_tokens, "tools": oa_tools, "tool_choice": "auto",
              "messages": [{"role": "system", "content": system}] + list(messages)}
        if timeout is not None:
            kw["timeout"] = timeout
        raw = client.chat.completions.with_raw_response.create(**kw)
        m = raw.parse()
        choice = m.choices[0]
        content = []
        for c in choice.message.tool_calls or []:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            content.append(_Block(type="tool_use", name=c.function.name, input=args if isinstance(args, dict) else {}))
        if choice.message.content:
            content.append(_Block(type="text", text=choice.message.content))
        u = m.usage
        usage = _Block(input_tokens=getattr(u, "prompt_tokens", 0) or 0, output_tokens=getattr(u, "completion_tokens", 0) or 0,
                       cache_read_input_tokens=0, cache_creation_input_tokens=0)
        resp = _Block(content=content, usage=usage, model=m.model)
        return _Block(headers={"x-orca-resolved-model": m.model}, parse=lambda: resp)

    return _Block(messages=_Block(with_raw_response=_Block(create=create)))


def default_factory(provider: str, timeout: float = 60.0):
    import anthropic

    if provider == "orca":
        key = os.environ.get("ORCA_API_KEY")
        if not key:
            raise LLMError("ORCA_API_KEY が未設定です")
        return anthropic.Anthropic(base_url=ORCA_BASE_URL, api_key=key, max_retries=0, timeout=timeout)  # 再試行は llm.call が行う
    if provider == "orca-openai":
        key = os.environ.get("ORCA_API_KEY")
        if not key:
            raise LLMError("ORCA_API_KEY が未設定です")
        import openai

        return openai_as_anthropic(openai.OpenAI(base_url=ORCA_BASE_URL + "/v1", api_key=key, max_retries=0, timeout=timeout))
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY が未設定です")
        ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")  # ワークスペースに紐づかないキーだけ必要
        return anthropic.Anthropic(api_key=key, max_retries=0, timeout=timeout,
                                   default_headers={"anthropic-workspace-id": ws} if ws else None)
    raise LLMError(f"未知のプロバイダ: {provider}")


@dataclass
class Result:
    tool_uses: list[dict] = field(default_factory=list)
    text: str = ""
    llm_id: str = ""
    provider: str = ""
    model: str = ""
    cost_usd: float | None = None


def _record(conn, org_id, purpose, provider, req_model, resolved, usage, cost, cost_status, status, error, latency):
    llm_id = db.new_id("llm_")
    db.run(conn, "INSERT INTO llm_call VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (llm_id, org_id, purpose, provider, req_model, resolved,
            usage.get("in", 0), usage.get("out", 0), usage.get("cache_read", 0), usage.get("cache_write", 0),
            cost, cost_status, status, error, round(latency, 2), db.now()))
    conn.commit()
    return llm_id


def call(conn, org_id: str | None, kind: str, system: str, tools: list[dict], messages: list[dict],
         client_factory=None, config: dict | None = None) -> Result:
    cfg = config or load_config()
    factory = client_factory or (lambda p: default_factory(p, cfg["timeout"]))
    base_model = cfg["models"][kind]
    errors, timeouts, others = [], 0, 0
    now = time.time()
    # 連続でタイムアウトした経路は、後回しにする（順番を入れ替えるだけ。ほかが使えなければ、最後に試す）
    def _req_model(p: str) -> str:
        if "/" in base_model:  # openai/… google/… deepseek/… など。Orca の名前をそのまま送る（Claude 以外の小型モデル）
            return base_model
        return ("anthropic/" + cfg.get("orca_model_names", {}).get(base_model, base_model)) if p == "orca" else base_model

    allowed = (cfg.get("provider_only") or {}).get(kind)
    routes = [p for p in cfg["providers"] if not allowed or p in allowed]
    # orca-openai は、orca と同じキーで使える OpenAI 互換の別の口（音声入力など、Anthropic 互換にないモデル用）。providers に書かなくても、
    # 「orca が使える」ときだけ、その種類の許可した経路として足す（Claude 直へは行かない）
    if allowed and "orca" in cfg["providers"]:
        routes += [a for a in allowed if a == "orca-openai" and a not in routes]
    order = sorted(routes, key=lambda p: (_soft_cooldown.get(p, 0.0) > now) or (_model_soft.get((p, _req_model(p)), 0.0) > now))
    timeout = float(cfg.get("timeouts", {}).get(kind, cfg["timeout"]))
    for provider in order:
        if _model_hard.get((provider, _req_model(provider)), 0.0) > time.time():
            errors.append(f"{provider}: {_req_model(provider)} は、混雑のため一時的に試していません")
            others += 1
            continue
        if _cooldown.get(provider, 0.0) > time.time():
            errors.append(f"{provider}: 使えない状態のため試していません（キー・残高・レート制限）")
            others += 1
            continue
        req_model = _req_model(provider)
        extra = _extra_params(cfg, kind, base_model, provider)
        raw = msg = None
        server_error = False
        for attempt in range(1 + max(0, int(cfg.get("retry_transient", 1)))):
            t0 = time.time()
            try:
                client = factory(provider)
                raw = client.messages.with_raw_response.create(
                    model=req_model, max_tokens=cfg["max_tokens"], system=system, tools=tools, messages=messages, timeout=timeout, **extra)
                msg = raw.parse()
                break
            except Exception as e:  # 4xx・5xx・通信・タイムアウト・キー未設定のすべてで、次の経路へ
                slow = is_timeout(e)
                err = f"{type(e).__name__}: {redact(e)}"
                _record(conn, org_id, kind, provider, req_model, None, {}, None, "—", "timeout" if slow else "error", err, time.time() - t0)
                errors.append(f"{provider}: {err}")
                if slow:
                    timeouts += 1
                    _timeout_streak[provider] = _timeout_streak.get(provider, 0) + 1
                    if _timeout_streak[provider] >= TIMEOUT_STREAK:
                        _soft_cooldown[provider] = time.time() + SOFT_COOLDOWN
                        _timeout_streak[provider] = 0
                    break  # 遅い経路で待ち直さない。次の経路へ
                if isinstance(e, LLMError):
                    break  # キー未設定など、設定の問題。通信なしで分かるので、故障として数えない
                others += 1
                if getattr(e, "status_code", None) == 429 and (is_free_error(e) or is_free_model(req_model)):
                    _model_hard[(provider, req_model)] = time.time() + _retry_after(e)  # そのモデルだけ。経路全体は止めない
                else:
                    _cool_down(provider, e)
                server_error = is_transient(e)
                if is_transient(e) and attempt < int(cfg.get("retry_transient", 1)) and provider not in _cooldown:
                    time.sleep(0.3)
                    continue
                break
        if msg is None:
            if server_error:
                k = (provider, req_model)
                _model_streak[k] = _model_streak.get(k, 0) + 1
                if _model_streak[k] >= MODEL_STREAK:
                    _model_soft[k] = time.time() + MODEL_SOFT_COOLDOWN
                    _model_streak[k] = 0
            continue
        _model_streak.pop((provider, req_model), None)
        _timeout_streak[provider] = 0
        headers = getattr(raw, "headers", {}) or {}
        resolved = headers.get("x-orca-resolved-model") or req_model
        u = msg.usage
        usage = {"in": getattr(u, "input_tokens", 0) or 0, "out": getattr(u, "output_tokens", 0) or 0,
                 "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0}
        cost = cost_usd(resolved, **{"in_tok": usage["in"], "out_tok": usage["out"],
                                     "cache_read": usage["cache_read"], "cache_write": usage["cache_write"]})
        status_cost = "計算済み" if cost is not None else "未計算"
        llm_id = _record(conn, org_id, kind, provider, req_model, resolved, usage, cost, status_cost,
                         "ok" if cost is not None else "uncosted", None, time.time() - t0)
        if cost is None:
            raise UncostedModelError(f"単価表にないモデルです: {resolved}")
        return Result(
            tool_uses=[{"name": b.name, "input": dict(b.input)} for b in msg.content if b.type == "tool_use"],
            text=" ".join(b.text for b in msg.content if b.type == "text")[:500],
            llm_id=llm_id, provider=provider, model=resolved, cost_usd=cost)
    if timeouts and not others:
        raise LLMTimeout("すべての経路がタイムアウトしました: " + " / ".join(errors))
    raise LLMError("すべての経路が失敗しました: " + " / ".join(errors))


def _extra_params(cfg: dict, kind: str, model: str, provider: str) -> dict:
    """設定した種類にだけ、応答の深さ（effort）・思考の切り替えを付ける。対応しないモデル・経路（Orca は未確認）には、送らない。"""
    if provider != "anthropic":
        return {}
    extra = {}
    effort = (cfg.get("effort_by_kind") or {}).get(kind)
    if effort and model in EFFORT_MODELS:
        extra["output_config"] = {"effort": effort}
    if kind in (cfg.get("thinking_off") or []) and model in THINKING_OFF_MODELS:
        extra["thinking"] = {"type": "disabled"}
    return extra


def cost_report(conn, since: float | None = None) -> list[dict]:
    """モデルごと・判断の種類ごとの、呼び出し数・トークン・原価。応答で解決されたモデル名で集計する。"""
    rows = db.many(conn, "SELECT COALESCE(resolved_model, req_model) AS model, purpose, provider, COUNT(*) AS calls, "
                         "SUM(status='error') AS errors, SUM(status='timeout') AS timeouts, SUM(in_tok) AS in_tok, SUM(out_tok) AS out_tok, "
                         "SUM(COALESCE(cost_usd,0)) AS cost_usd, SUM(cost_status='未計算') AS uncosted "
                         "FROM llm_call WHERE created_at>=? GROUP BY 1, 2, 3 ORDER BY cost_usd DESC", (since or 0,))
    return [dict(r) for r in rows]
