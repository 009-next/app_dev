"""会話から次の業務を考える判断。単発と、上限つきのループ。

- 単発: 会話・画面・記録を全部渡して、1 回で提案（従来の `decide` と同じ型）。
- ループ: AI が、読み取り専用の道具（記録・画面の分析結果）で調べ直し、コードの点検で捨てられたら直し、作り手の返答（違う・答え）を受けて計画を直す。
  止める条件はコードが決める（反復・費用・同じ調べ物の繰り返し）。**書き込みの道具はない**（提案だけ）。
- 上のモデルへ進むのは、無効な応答・実在しない引用・確信度がすべて low のとき（安い順から）。
- 実測と設計: `../your_folder/音声分析_検討.md`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import agent, db, llm, talk, talk_specs
from .objects import object_history

DEFAULT_TIERS = ("decide_light", "decide_heavy")
MAX_ITER = 4
BUDGET_USD = 0.25
MAX_READS = 2


@dataclass
class Outcome:
    result: dict | None = None
    kind: str = "none"          # propose / ask / none / failed
    model: str = ""
    iterations: int = 0
    cost: float = 0.0
    trace: list = field(default_factory=list)


def _tiers(cfg: dict, allow_external: bool) -> list[str]:
    tiers = list((cfg.get("talk_tiers") or DEFAULT_TIERS))
    if not allow_external:
        tiers = [t for t in tiers if t not in cfg.get("external_kinds", ())]  # 第三者のモデルは、門を通ったときだけ
    return tiers or list(DEFAULT_TIERS)


def _call(conn, org_id, tier, tools, user, client_factory, cfg):
    return llm.call(conn, org_id, tier, talk_specs.TALK_SYSTEM, tools, [{"role": "user", "content": user}], client_factory, cfg)


def _pick(res, names):
    return next((t for t in res.tool_uses if t["name"] in names), None)


def _all_low(result: dict) -> bool:
    return result["tool"] == "propose_next_steps" and all(s["confidence"] == "low" for s in result["steps"])


def analyze_single(conn, org_id, turns, screen, records, *, corrections=None, client_factory=None, config=None,
                   allow_external=False, budget=BUDGET_USD) -> Outcome:
    cfg = config or llm.load_config()
    out, tnorm = Outcome(), talk.transcript_norm(turns)
    names = talk.person_names(" ".join(x["text"] for x in turns))
    user = talk.render_input(turns, screen, records, corrections=corrections)
    names = {t["name"] for t in talk_specs.ANSWER_TOOLS}
    for tier in _tiers(cfg, allow_external):
        out.iterations += 1
        try:
            res = _call(conn, org_id, tier, talk_specs.ANSWER_TOOLS, user, client_factory, cfg)
        except Exception as e:  # noqa: BLE001  API 失敗・単価不明などは、次のモデルへ
            out.trace.append({"tier": tier, "error": llm.redact(e, 120) if hasattr(llm, "redact") else str(e)[:120]})
            continue
        out.cost += res.cost_usd or 0
        out.model = res.model
        tu = _pick(res, names)
        result, why = talk.validate(tu["name"], tu["input"], tnorm, names) if tu else (None, ["ツールが呼ばれなかった"])
        out.trace.append({"tier": tier, "model": res.model, "tool": tu["name"] if tu else None, "ok": bool(result), "why": why,
                          "cost": round(res.cost_usd or 0, 5)})
        if result and not (_all_low(result) and tier != _tiers(cfg, allow_external)[-1]):
            out.result, out.kind = result, {"propose_next_steps": "propose", "ask_clarifying_question": "ask", "no_action": "none"}[result["tool"]]
            return out
        if out.cost > budget:
            break
    out.kind = "failed"
    return out


def analyze_loop(conn, org_id, turns, screen, records_fn, *, corrections=None, client_factory=None, config=None,
                 allow_external=False, max_iter=MAX_ITER, budget=BUDGET_USD) -> Outcome:
    """screen: 画面の分析結果の文字（なければ None）。records_fn(topic) -> list[str]（見られる記録だけ）。"""
    cfg = config or llm.load_config()
    out, tnorm = Outcome(), talk.transcript_norm(turns)
    names = talk.person_names(" ".join(x["text"] for x in turns))
    tiers = _tiers(cfg, allow_external)
    ti, notes, used, escalated_low = 0, [], set(), False
    answer = {t["name"] for t in talk_specs.ANSWER_TOOLS}
    for _ in range(max_iter):
        avail = [t for t in talk_specs.READ_TOOLS if t["name"] not in used and len(used) < MAX_READS
                 and (t["name"] != "read_screen_result" or screen is not None)]
        hint = ("利用できる資料: " + "、".join(x for x in ("その物の記録（read_records）" if "read_records" not in used else "",
                                                          "通話の画面の分析結果（read_screen_result）" if screen is not None and "read_screen_result" not in used else "") if x)
                + "。必要なら、提案の前に読んでください。") if avail else None
        user = talk.render_input(turns, None, None, notes=notes, corrections=corrections, records_hint=hint)
        out.iterations += 1
        tier = tiers[min(ti, len(tiers) - 1)]
        try:
            res = _call(conn, org_id, tier, talk_specs.ANSWER_TOOLS + avail, user, client_factory, cfg)
        except Exception as e:  # noqa: BLE001
            out.trace.append({"tier": tier, "error": str(e)[:120]})
            ti += 1
            if ti > len(tiers):
                break
            continue
        out.cost += res.cost_usd or 0
        out.model = res.model
        tu = res.tool_uses[0] if res.tool_uses else None
        rec = {"tier": tier, "model": res.model, "tool": tu["name"] if tu else None, "cost": round(res.cost_usd or 0, 5)}
        out.trace.append(rec)
        if out.cost > budget:
            rec["stop"] = "費用の上限"
            break
        if tu is None:
            ti += 1
            continue
        if tu["name"] in ("read_records", "read_screen_result"):
            if tu["name"] in used or tu["name"] not in {t["name"] for t in avail}:
                rec["stop"] = "同じ調べ物の繰り返し"
                break
            used.add(tu["name"])
            if tu["name"] == "read_records":
                rows = records_fn(str(tu["input"].get("topic", ""))) if records_fn else []
                notes.append("read_records → " + ("\n".join(f"- {r}" for r in rows) if rows else "記録はありません"))
            else:
                notes.append(f"read_screen_result → {screen}")
            rec["read"] = tu["name"]
            continue
        if tu["name"] not in answer:
            ti += 1
            continue
        result, why = talk.validate(tu["name"], tu["input"], tnorm, names)
        rec.update(ok=bool(result), why=why)
        if result is None:
            notes.append("【点検】前の答えは捨てました: " + "；".join(why) + "。会話の文字から、そのまま引用して、直してください。")
            ti += 1
            continue
        if _all_low(result) and not escalated_low and ti < len(tiers) - 1:
            escalated_low = True
            ti += 1
            notes.append("【点検】確信度がすべて low でした。根拠を確かめ直してください。")
            rec["escalate"] = "確信度が低い"
            continue
        out.result, out.kind = result, {"propose_next_steps": "propose", "ask_clarifying_question": "ask", "no_action": "none"}[result["tool"]]
        return out
    out.kind = "failed"
    return out


# ---- アプリからの実行（jobs に任せる）-------------------------------------------------------------

def screen_text(conn, actor, obj_id: str) -> str | None:
    """作り手が確認済み（結果が採用された）の、通話の画面の分析結果（文字だけ・見られるカードだけ）。"""
    for c in reversed(object_history(conn, actor, obj_id)):
        v = db.one(conn, "SELECT result FROM card_vision WHERE card_id=? AND org_id=? AND status='done' AND result IS NOT NULL "
                         "ORDER BY created_at DESC LIMIT 1", (c["card_id"], actor.org_id))
        if v:
            r = json.loads(v["result"])
            claims = "／".join(w["claim"] for w in r.get("work_inference", []))
            return f"{r.get('visible_summary', '')}" + (f"（推論: {claims}）" if claims else "")
    return None


def records_of(conn, actor, obj_id: str, limit: int = 8) -> list[str]:
    rows = []
    for c in object_history(conn, actor, obj_id)[-limit:]:
        rows.append(f"{(c['title'] or '')}: 作業前 {(c['before_desc'] or '')[:80]}／作業後 {(c['after_desc'] or '')[:80]}")
    return rows


def start(conn, actor, session_id: str, *, jobs, client_factory=None, config=None, mode: str | None = None,
          parent_plan_id: str | None = None, corrections: list[str] | None = None) -> str:
    """確認済みの文字だけで、提案を作る。単発かループかは mode（既定は config['talk_mode']＝単発。実測で、ループは同意率を上げなかったため）。"""
    s = talk._session(conn, actor, session_id)
    if not talk.enabled(conn, actor.org_id):
        raise talk.TalkRefused("この組織では、会話から次を考える機能がオフです")
    if s["status"] != "open":
        raise talk.TalkRefused("この会話は、もう閉じています")
    segs = talk.segments(conn, session_id)
    if not segs:
        raise talk.TalkRefused("会話の文字がありません")
    if any(not r["reviewed"] for r in segs):
        raise talk.TalkRefused("文字を確認してから、AI に渡します。確認していない区切りがあります")
    if db.one(conn, "SELECT COUNT(*) c FROM talk_plan WHERE session_id=?", (session_id,))["c"] >= talk.MAX_RUNS_PER_SESSION:
        raise talk.TalkRefused(f"1つの会話で AI に考えさせられるのは {talk.MAX_RUNS_PER_SESSION} 回までです")
    if db.one(conn, "SELECT 1 FROM talk_plan WHERE session_id=? AND status='running' AND created_at>?", (session_id, db.now() - 180)):
        raise talk.TalkRefused("AI が考えています")
    cfg = config or llm.load_config()
    mode = mode or cfg.get("talk_mode") or "single"
    turns = [{"who": r["who"], "text": r["text"]} for r in segs]
    screen = screen_text(conn, actor, s["obj_id"])
    ext_ok, _ = talk.external_ok_talk(conn, actor.org_id, s, turns, screen)
    pid = db.new_id("tkp_")
    db.run(conn, "INSERT INTO talk_plan(plan_id, session_id, org_id, created_at, status, parent_plan_id, mode) VALUES(?,?,?,?, 'running', ?, ?)",
           (pid, session_id, actor.org_id, db.now(), parent_plan_id, mode))
    db.audit(conn, actor.org_id, "talk.analyze_start", actor.member_id, pid, f"{mode}／確認済みの文字 {len(turns)} 区切り")
    conn.commit()
    org_id, member_id, obj_id = actor.org_id, actor.member_id, s["obj_id"]

    def job(job_conn):
        _run(job_conn, actor, pid, session_id, obj_id, turns, screen, mode, corrections, ext_ok, client_factory, cfg)

    jobs.submit(conn, job)
    return pid


MAX_CONSECUTIVE_DISAGREE = 2


def revise(conn, actor, plan_id: str, *, jobs, client_factory=None, config=None) -> str:
    """『違う』『一部違う』、または質問への答えを材料に、計画を直して、もう一度提案する。
    続けて 2 回『違う』なら、AI は食い下がらず、ここで止める（作り手が自分で入力する）。"""
    p = talk.plan_of(conn, actor, plan_id)
    if p["status"] not in ("disagreed", "partial", "answered"):
        raise talk.TalkRefused("直せるのは、『違う』『一部違う』の提案と、答えた質問だけです")
    chain, disagrees, cur = [], 0, p
    while cur is not None:
        if cur["feedback"]:
            chain.append(cur["feedback"])
        disagrees += 1 if cur["status"] in ("disagreed",) else 0
        cur = db.one(conn, "SELECT * FROM talk_plan WHERE plan_id=?", (cur["parent_plan_id"],)) if cur["parent_plan_id"] else None
    if disagrees >= MAX_CONSECUTIVE_DISAGREE:
        raise talk.TalkRefused("続けて『違う』でした。AI の提案はここまでにします。次の業務は、ご自身で入力してください")
    return start(conn, actor, p["session_id"], jobs=jobs, client_factory=client_factory, config=config, mode=p["mode"],
                 parent_plan_id=plan_id, corrections=list(reversed(chain)))


def _run(conn, actor, pid, session_id, obj_id, turns, screen, mode, corrections, ext_ok, client_factory, cfg) -> None:
    try:
        if mode == "single":
            out = analyze_single(conn, actor.org_id, turns, screen, records_of(conn, actor, obj_id), corrections=corrections,
                                 client_factory=client_factory, config=cfg, allow_external=ext_ok)
        else:
            out = analyze_loop(conn, actor.org_id, turns, screen, lambda topic: records_of(conn, actor, obj_id), corrections=corrections,
                               client_factory=client_factory, config=cfg, allow_external=ext_ok)
    except Exception as e:  # noqa: BLE001  失敗しても、会話・カードには影響しない
        out = Outcome(kind="failed", trace=[{"error": f"{type(e).__name__}: {str(e)[:120]}"}])
    status = "proposed" if out.kind in ("propose", "ask") else ("none" if out.kind == "none" else "failed")
    db.run(conn, "UPDATE talk_plan SET model=?, kind=?, result=?, status=?, iterations=?, trace=?, cost_usd=? WHERE plan_id=?",
           (out.model, out.kind, json.dumps(out.result, ensure_ascii=False) if out.result else None, status, out.iterations,
            json.dumps(out.trace, ensure_ascii=False), round(out.cost, 6), pid))
    db.audit(conn, actor.org_id, "talk.analyze", actor.member_id, pid, f"{out.kind}／{out.model or '-'}／反復{out.iterations}")
    conn.commit()
