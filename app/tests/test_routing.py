"""モデルの切り替え（Haiku → Sonnet → Opus）・同じ応答の複数ツール（G-1）・経路の一時停止・原価の記録のテスト。"""

import pytest

from app import agent, cards, db, llm, notifications, objects, summaries
from app.tests.fakes import client_dynamic, client_failing

ORG = "org_1"
C = {"reason": "r", "evidence": ""}
GOOD_TYPE = ("select_card_type", {**C, "type_id": "cleaning"})
BAD_TYPE = ("select_card_type", {**C})  # 必須の type_id が欠けた、形式の崩れた呼び出し
INPUTS = {"状況": "ソファのクリーニングを実施した"}


def by_model(table):
    """依頼したモデルの名前（haiku / sonnet / opus を含む）ごとに、応答を切り替える fake。table: {"haiku": [...], ...}"""
    def fn(kw, i):
        for key, resp in table.items():
            if key in kw["model"]:
                return resp
        raise AssertionError("想定外のモデル: " + kw["model"])
    return client_dynamic(fn, echo_model=True)


def models_called(f):
    return [c["model"] for c in f.state["calls"]]


@pytest.fixture
def conn(conn):
    return conn


def decide(conn, f, stage="classify", inputs=None, **kw):
    return agent.decide(conn, ORG, stage=stage, inputs=inputs or INPUTS, card_id="card_1", client_factory=f, **kw)


# ---- 段の選び方 ---------------------------------------------------------------------

def test_default_tiers_per_stage_and_overrides():
    cfg = llm.load_config()
    assert agent.routing_tiers("classify", None, cfg) == ["triage", "decide_light", "decide_heavy"]
    assert agent.routing_tiers("summary", None, cfg) == ["decide_light", "decide_heavy"]
    assert agent.routing_tiers("periodic", None, cfg) == ["decide_light", "decide_heavy"]
    assert agent.routing_tiers("classify", "decide_heavy", cfg) == ["decide_heavy"]  # 種類を明示したら、切り替えない（従来どおり）
    off = {**cfg, "routing": {"enabled": False}}
    assert agent.routing_tiers("classify", None, off) == ["decide_light"] and agent.routing_tiers("privacy", None, off) == ["decide_heavy"]
    no_triage = {**cfg, "models": {k: v for k, v in cfg["models"].items() if k != "triage"}}
    assert agent.routing_tiers("classify", None, no_triage) == ["decide_light", "decide_heavy"]  # モデルの設定がない段は使わない


def test_models_and_orca_names():
    cfg = llm.load_config()
    assert cfg["models"]["triage"] == "claude-haiku-4-5" and cfg["models"]["decide_heavy"] == "claude-opus-5"
    assert cfg["providers"][0] == "orca"  # Orca を優先


# ---- 切り替え -----------------------------------------------------------------------

def test_haiku_first_and_accepted_when_valid(conn):
    f = by_model({"haiku": [GOOD_TYPE]})
    d = decide(conn, f)
    assert models_called(f) == ["anthropic/claude-haiku-4.5"]  # Orca 側の名前。1回だけ
    assert (d.tool, d.default_used, len(d.tiers)) == ("select_card_type", False, 1)
    assert d.cost_usd == pytest.approx(llm.cost_usd("claude-haiku-4-5", 100, 20))


def test_escalates_haiku_to_sonnet_on_broken_call_and_sums_cost(conn):
    f = by_model({"haiku": [BAD_TYPE], "sonnet": [GOOD_TYPE]})
    d = decide(conn, f)
    assert [m.split("/")[-1] for m in models_called(f)] == ["claude-haiku-4.5", "claude-sonnet-5"]
    assert d.tool == "select_card_type" and not d.default_used and d.model == "claude-sonnet-5"
    assert d.cost_usd == pytest.approx(llm.cost_usd("claude-haiku-4-5", 100, 20) + llm.cost_usd("claude-sonnet-5", 100, 20))
    assert d.tiers[0]["escalated_because"].startswith("無効な応答")
    log = db.one(conn, "SELECT validation, cost_usd FROM decision_log")
    assert "モデルの切り替え" in log["validation"] and log["cost_usd"] == pytest.approx(d.cost_usd)
    assert db.one(conn, "SELECT COUNT(*) c FROM llm_call")["c"] == 2


def test_escalates_all_the_way_to_opus_then_defaults_if_still_invalid(conn):
    f = by_model({"haiku": [BAD_TYPE], "sonnet": [BAD_TYPE], "opus": [GOOD_TYPE]})
    assert decide(conn, f).model == "claude-opus-5" and len(f.state["calls"]) == 3
    f2 = by_model({"haiku": [BAD_TYPE], "sonnet": [BAD_TYPE], "opus": [BAD_TYPE]})
    d = decide(conn, f2)
    assert d.default_used and (d.tool, d.args) == agent.DEFAULTS["classify"] and len(f2.state["calls"]) == 3


def test_escalates_when_evidence_is_not_in_the_input_unless_no_action(conn):
    fake_ev = {**C, "evidence": "入力にない引用", "type_id": "cleaning"}
    f = by_model({"haiku": [("select_card_type", fake_ev)], "sonnet": [("select_card_type", {**C, "evidence": "ソファのクリーニングを実施した", "type_id": "cleaning"})]})
    d = decide(conn, f)
    assert len(f.state["calls"]) == 2 and d.evidence_ok is True and d.tiers[0]["escalated_because"] == "裏付けが入力に見つからない"
    f2 = by_model({"haiku": [("no_action", {"reason": "r", "evidence": "入力にない引用"})]})
    d2 = decide(conn, f2)  # no_action は、裏付けが弱くても切り替えない（何もしない判断で、費用を増やさない）
    assert len(f2.state["calls"]) == 1 and d2.tool == "no_action"


def test_sensitive_privacy_no_action_escalates_but_action_is_accepted(conn):
    text = {"状況": "玄関に、入居者の氏名と住所が印字された郵便物が置かれている"}
    f = by_model({"haiku": [("no_action", C)], "sonnet": [("propose_extra_mask", {**C, "target": "郵便物の宛名"})]})
    d = decide(conn, f, stage="privacy", inputs=text)
    assert d.tool == "propose_extra_mask" and d.model == "claude-sonnet-5"
    assert d.tiers[0]["escalated_because"] == "機微な語があるのに何もしない判断"
    f2 = by_model({"haiku": [("propose_extra_mask", {**C, "target": "郵便物の宛名"})]})
    assert len(decide(conn, f2, stage="privacy", inputs=text).tiers) == 1  # 提案していれば、Haiku の結果を使う
    f3 = by_model({"haiku": [("no_action", C)]})
    assert len(decide(conn, f3, stage="privacy", inputs={"状況": "壁紙を張り替えた"}).tiers) == 1  # 機微な語がなければ、何もしないで確定


def test_summary_and_periodic_start_at_sonnet_never_haiku(conn):
    for stage, resp in (("summary", ("hold_summary", C)), ("periodic", ("no_action", C))):
        f = by_model({"sonnet": [resp]})
        decide(conn, f, stage=stage)
        assert "haiku" not in " ".join(models_called(f))


def test_upper_tier_failure_keeps_the_lower_valid_decision(conn):
    weak = ("select_card_type", {**C, "evidence": "入力にない引用", "type_id": "cleaning"})
    calls = []

    def fn(kw, i):
        calls.append(kw["model"])
        if "haiku" in kw["model"]:
            return [weak]
        raise RuntimeError("Sonnet が落ちた")
    d = decide(conn, client_dynamic(fn, echo_model=True))
    assert d.tool == "select_card_type" and not d.default_used
    assert d.model.startswith("claude-haiku")  # 上位が落ちたので、Haiku の有効な結果をそのまま使う
    assert d.tiers[0]["escalated_because"] and "error" in d.tiers[1]


def test_llm_call_cap_stops_escalation(conn):
    f = by_model({"haiku": [BAD_TYPE], "sonnet": [GOOD_TYPE]})
    ctx = agent.RunCtx(max_llm_calls=1)
    d = decide(conn, f, ctx=ctx)
    assert len(f.state["calls"]) == 1 and d.default_used
    assert ctx.calls == 1 and ctx.llm_calls == 1  # 「判断」の数（MAX_CALLS）と、LLM を呼んだ数は、別に数える


def test_disabled_routing_and_explicit_kind_behave_as_before(conn):
    cfg = {**llm.load_config(), "routing": {"enabled": False}}
    f = by_model({"sonnet": [GOOD_TYPE]})
    decide(conn, f, config=cfg)
    assert models_called(f) == ["anthropic/claude-sonnet-5"]
    f2 = by_model({"opus": [BAD_TYPE]})
    d = decide(conn, f2, kind="decide_heavy")
    assert models_called(f2) == ["anthropic/claude-opus-5"] and d.default_used  # 無効でも切り替えない


def test_describe_quality_high_uses_the_top_model(conn):
    write = ("write_card_text", {"title": "t", "changes": ["c"], "description": "d"})
    f = client_dynamic(lambda kw, i: [write], echo_model=True)
    ctx = agent.RunCtx()
    agent.describe(conn, ORG, INPUTS, ctx, f)
    agent.describe(conn, ORG, INPUTS, ctx, f, quality="high")
    assert [m.split("/")[-1] for m in models_called(f)] == ["claude-sonnet-5", "claude-opus-5"]


# ---- 同じ応答の複数ツール（G-1） -----------------------------------------------------------

DRAFT = ("draft_notification", {**C, "recipient": "オーナー", "message": "内容が食い違っています。確認してください。"})


def test_first_valid_tool_is_the_decision_and_draft_extra_is_executed(conn):
    f = by_model({"sonnet": [("hold_summary", C), DRAFT]})
    d = decide(conn, f, stage="summary", recipient_ok=lambda r, m: True)
    assert d.tool == "hold_summary" and not d.default_used
    assert d.extras == [{"tool": "draft_notification", "args": DRAFT[1], "valid": True, "executed": True}]
    assert "下書きとして実行" in db.one(conn, "SELECT validation FROM decision_log")["validation"]


def test_invalid_leading_tool_is_skipped_for_a_valid_later_one(conn):
    f = by_model({"haiku": [BAD_TYPE, ("no_action", C)]})
    d = decide(conn, f)
    assert d.tool == "no_action" and not d.default_used and len(f.state["calls"]) == 1  # 切り替えずに済む


def test_other_extras_are_logged_but_never_executed(conn):
    f = by_model({"haiku": [GOOD_TYPE, ("propose_narrower_scope", {**C, "scope": "org_only"})]})
    d = decide(conn, f, current_scope="link_30d")
    assert d.tool == "select_card_type"
    assert d.extras[0]["tool"] == "propose_narrower_scope" and d.extras[0]["executed"] is False


def test_second_draft_and_leaking_draft_are_not_executed(conn):
    f = by_model({"sonnet": [DRAFT, DRAFT]})
    d = decide(conn, f, stage="periodic", recipient_ok=lambda r, m: True)
    assert d.tool == "draft_notification" and d.extras[0]["executed"] is False  # 下書きは1つだけ
    leak = ("draft_notification", {**C, "recipient": "担当者", "message": "秘密の内容"})
    f2 = by_model({"sonnet": [("hold_summary", C), leak]})
    d2 = decide(conn, f2, stage="summary", recipient_ok=lambda r, m: False)
    assert d2.extras[0]["valid"] is False and d2.extras[0]["executed"] is False


def test_forbidden_tool_in_a_multi_call_still_blocks_everything(conn):
    f = by_model({"haiku": [GOOD_TYPE, ("publish_share_link", {**C, "scope": "link_30d"})], "sonnet": [GOOD_TYPE], "opus": [GOOD_TYPE]})
    d = decide(conn, f)  # 許可されていないツールが混ざる応答は、実行しない。上位のモデルに切り替える
    assert d.model == "claude-sonnet-5" and d.tiers[0]["escalated_because"].startswith("無効な応答") and d.extras == []


def test_summary_refresh_saves_the_extra_draft(conn, alice, bob):
    o = objects.register_object(conn, alice, "空調")[0]
    cards.create_card(conn, alice, o["obj_id"], before_desc="配管を点検した", client_factory=client_failing(RuntimeError("x")), scope="org_only")
    f = by_model({"sonnet": [("hold_summary", C), DRAFT]})
    summaries.refresh(conn, alice.org_id, o["obj_id"], client_factory=f)
    assert [(r["recipient"], r["status"]) for r in db.many(conn, "SELECT * FROM notification")] == [("オーナー", "draft")]


# ---- 経路の優先と一時停止 --------------------------------------------------------------------

class Http(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(f"HTTP {status}")
        self.status_code = status
        self.response = type("R", (), {"headers": {"retry-after": str(retry_after)} if retry_after else {}})()


def two_routes(orca_error):
    """orca は常に orca_error、anthropic は成功する factory。呼ばれた経路を .used に残す。"""
    used = []
    ok = client_dynamic(lambda kw, i: [("no_action", C)], echo_model=True)

    def factory(provider):
        used.append(provider)
        if provider == "orca":
            def create(**kw):
                raise orca_error
            return type("C", (), {"messages": type("M", (), {"with_raw_response": type("W", (), {"create": staticmethod(create)})})})()
        return ok(provider)
    factory.used = used
    return factory


def test_orca_is_tried_first_and_a_402_route_is_paused_then_reused_after_reset(conn):
    f = two_routes(Http(402))
    llm.call(conn, ORG, "decide_light", "s", [agent.SPEC["tools"]["no_action"]], [{"role": "user", "content": "x"}], f)
    assert f.used == ["orca", "anthropic"]  # まず Orca、失敗したら Claude API 直へ
    llm.call(conn, ORG, "decide_light", "s", [agent.SPEC["tools"]["no_action"]], [{"role": "user", "content": "x"}], f)
    assert f.used == ["orca", "anthropic", "anthropic"]  # Orca は、しばらく試さない（無駄な失敗の記録を増やさない）
    assert db.one(conn, "SELECT COUNT(*) c FROM llm_call WHERE status='error'")["c"] == 1
    llm.reset_cooldowns()
    llm.call(conn, ORG, "decide_light", "s", [agent.SPEC["tools"]["no_action"]], [{"role": "user", "content": "x"}], f)
    assert f.used[-2:] == ["orca", "anthropic"]  # 使えるようになれば、また Orca を優先


def test_429_pauses_only_for_retry_after_and_500_never_pauses(conn):
    llm.call(conn, ORG, "decide_light", "s", [agent.SPEC["tools"]["no_action"]], [{"role": "user", "content": "x"}], two_routes(Http(429, 30)))
    import time
    left = llm._cooldown["orca"] - time.time()
    assert 0 < left <= 30
    llm.reset_cooldowns()
    llm.call(conn, ORG, "decide_light", "s", [agent.SPEC["tools"]["no_action"]], [{"role": "user", "content": "x"}], two_routes(Http(500)))
    assert "orca" not in llm._cooldown  # 一時的な故障は、停止にしない


def test_model_names_per_route(conn):
    seen = []

    def fn(kw, i):
        seen.append(kw["model"])
        return [("no_action", C)]
    f = client_dynamic(fn, echo_model=True)
    llm.call(conn, ORG, "triage", "s", [agent.SPEC["tools"]["no_action"]], [{"role": "user", "content": "x"}], f)
    llm.call(conn, ORG, "triage", "s", [agent.SPEC["tools"]["no_action"]], [{"role": "user", "content": "x"}], f, {**llm.load_config(), "providers": ["anthropic"]})
    assert seen == ["anthropic/claude-haiku-4.5", "claude-haiku-4-5"]


# ---- 原価の記録 ---------------------------------------------------------------------------------

def test_cost_report_groups_by_model_and_matches_decision_costs(conn):
    f = by_model({"haiku": [BAD_TYPE], "sonnet": [GOOD_TYPE]})
    d = decide(conn, f)
    rep = {r["model"].split("/")[-1]: r for r in llm.cost_report(conn)}
    assert set(rep) == {"claude-haiku-4.5", "claude-sonnet-5"} or set(rep) == {"claude-haiku-4-5", "claude-sonnet-5"}
    assert sum(r["cost_usd"] for r in rep.values()) == pytest.approx(d.cost_usd)
    assert all(r["calls"] == 1 and r["uncosted"] == 0 for r in rep.values())


# ---- 実測で見つけた、無駄な切り替えの抑止（2026-09-20 の待ち時間の測定） -----------------------------------

def test_sensitive_no_action_is_only_doubted_once(conn):
    text = {"状況": "玄関に、入居者の氏名と住所が印字された郵便物が置かれている"}
    f = by_model({"haiku": [("no_action", C)], "sonnet": [("no_action", C)], "opus": [("propose_extra_mask", {**C, "target": "宛名"})]})
    d = decide(conn, f, stage="privacy", inputs=text)
    assert len(f.state["calls"]) == 2 and d.tool == "no_action" and d.model == "claude-sonnet-5"  # 2つのモデルが一致したら、3つ目（Opus）は呼ばない


def test_replaced_recipient_draft_does_not_escalate(conn):
    leak = ("draft_notification", {**C, "recipient": "担当者", "message": "担当者宛ての案"})
    f = by_model({"sonnet": [leak], "opus": [leak]})
    d = decide(conn, f, stage="summary", recipient_ok=lambda r, m: False)  # 宛先の検査に通らない（担当者がいない、など）
    assert len(f.state["calls"]) == 1  # 一般的な文面・オーナー宛てに置き換えて終わり。Opus に切り替えない
    assert d.boundary_violation and d.args["recipient"] == "オーナー" and d.args["message"] == agent.GENERIC_NOTICE


def test_real_invalid_calls_still_escalate_after_the_change(conn):
    f = by_model({"haiku": [BAD_TYPE], "sonnet": [GOOD_TYPE]})
    assert decide(conn, f).model == "claude-sonnet-5"  # 形式の崩れなど、上位で直る可能性があるものは、これまでどおり切り替える
