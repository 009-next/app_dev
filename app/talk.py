"""会話（マイク・スピーカー・手入力）から、次の業務を提案する。既定はオフ。作り手が確認して、同意して、承認して初めて動く。

流れ: セッション（告知の確認）→ 文字（区切りごと）→ **作り手が確認・修正・削除**（ここまで AI に渡さない）→ 提案（単発またはループ）
      → 「私はこう理解しました」に、合っている／違う／一部違う → 「合っている」の提案だけ、承認して下書きまで（送信・公開・共有範囲の拡大はしない）。

- 音声は保存しない。端末内で文字にした結果（または、音声分析の結果の文字）だけがここに来る。
- AI が受け取るのは、確認済みの文字・作り手が確認済みの画面の分析（文字）・見られる記録だけ。**画像も音声も渡さない。**
- 提案の根拠は、会話の文字からの引用。コードが、実在することを照合する。実在しない提案は捨てる。
- 会話は個人情報を含みやすい。第三者のモデルへ渡すかどうかは、門（`external_ok_talk`）が決める。
- 設計と実測: `../your_folder/音声分析_検討.md`。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re

from . import agent, db, llm, notifications, signals, talk_specs, vision
from .objects import NotFound, get_object_for, require

SOURCES = ("mic", "screen_audio", "typed", "dictation", "audio_analysis")
MAX_SEGMENTS = 200
MAX_TEXT = 500
MAX_TOTAL_CHARS = 6000
MAX_RUNS_PER_SESSION = 5
RETENTION_DAYS = 7
DIGITS = re.compile(r"\d{4,}|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+")
# 会話の指示に「従った」形跡。提案の文章（下書きの文面など）に出たときだけ見る。会話の引用や、「指示があったが従わない」という説明は、従ったことにならない
FOLLOWED = re.compile(r"全員に公開|公開してください|公開します|全員に共有|共有範囲を(?:広げ|全員)")


class TalkRefused(ValueError):
    """条件を満たさないので、実行しない（理由は作り手に見せてよい文）。"""


# ---- 設定・権限 -------------------------------------------------------------------------------

def enabled(conn, org_id: str) -> bool:
    if os.environ.get("MIRUCON_TALK") == "0":
        return False
    row = db.one(conn, "SELECT talk_llm FROM org_setting WHERE org_id=?", (org_id,))
    return row is not None and bool(row["talk_llm"])  # 既定はオフ


def audio_enabled(conn, org_id: str) -> bool:
    """音声分析（音声を音声対応モデルへ渡す第2の経路）。文字起こし（端末内）が標準で、これは別のスイッチ。"""
    if os.environ.get("MIRUCON_TALK_AUDIO") == "0" or not enabled(conn, org_id):
        return False
    row = db.one(conn, "SELECT talk_audio FROM org_setting WHERE org_id=?", (org_id,))
    return row is not None and bool(row["talk_audio"])


def may_use(actor, session) -> bool:
    """セッションを操作できるのは、作り手（開始した人）か、組織のオーナーだけ。"""
    return actor.kind == "member" and actor.org_id == session["org_id"] and (session["actor_id"] == actor.member_id or actor.role == "owner")


def _session(conn, actor, session_id: str):
    s = db.one(conn, "SELECT * FROM talk_session WHERE org_id=? AND session_id=?", (actor.org_id, session_id)) if actor.org_id else None
    if s is None:
        raise NotFound(session_id)
    if not may_use(actor, s):
        raise TalkRefused("会話を操作できるのは、開始した人か、オーナーだけです")
    return s


def purge_old(conn, days: int = RETENTION_DAYS) -> int:
    """保持期間を過ぎた会話の文字と提案を削除する（音声は、そもそも保存していない）。"""
    cutoff = db.now() - days * 86400
    ids = [r["session_id"] for r in db.many(conn, "SELECT session_id FROM talk_session WHERE created_at<?", (cutoff,))]
    for sid in ids:
        db.run(conn, "DELETE FROM talk_segment WHERE session_id=?", (sid,))
        db.run(conn, "DELETE FROM talk_plan WHERE session_id=?", (sid,))
        db.run(conn, "DELETE FROM talk_session WHERE session_id=?", (sid,))
    if ids:
        db.audit(conn, "-", "talk.purge", "system", "", f"{len(ids)}件（{days}日を過ぎた会話の文字）")
        conn.commit()
    return len(ids)


# ---- セッション・文字 -------------------------------------------------------------------------

def create_session(conn, actor, obj_id: str, *, source: str, consent: bool) -> str:
    obj = get_object_for(conn, actor, obj_id)
    require(actor, "create_edit_card", dict(obj))
    if os.environ.get("MIRUCON_TALK") == "0":
        raise TalkRefused("緊急停止中です")
    if not enabled(conn, actor.org_id):
        raise TalkRefused("この組織では、会話から次を考える機能がオフです（オーナーが設定します）")
    if source not in SOURCES:
        raise TalkRefused("集音の方法が正しくありません")
    if source == "audio_analysis" and not audio_enabled(conn, actor.org_id):
        raise TalkRefused("音声分析（音声を外部のモデルへ渡す経路）は、この組織ではオフです")
    if not consent:
        raise TalkRefused("会話の相手に、話を文字にすることを伝えたか、確認が必要です")
    purge_old(conn)
    sid = db.new_id("tks_")
    db.run(conn, "INSERT INTO talk_session(session_id, org_id, obj_id, actor_id, created_at, source, consent_ack, status) "
                 "VALUES(?,?,?,?,?,?,1,'open')", (sid, actor.org_id, obj_id, actor.member_id, db.now(), source))
    db.audit(conn, actor.org_id, "talk.start", actor.member_id, sid, f"出どころ={source}／相手への告知を確認")
    conn.commit()
    return sid


def add_segments(conn, actor, session_id: str, turns: list[dict]) -> int:
    """文字にした会話を、区切りごとに保存する（作り手の確認は、まだ）。"""
    s = _session(conn, actor, session_id)
    if s["status"] != "open":
        raise TalkRefused("この会話は、もう閉じています")
    n = db.one(conn, "SELECT COUNT(*) c, COALESCE(SUM(LENGTH(text)),0) t FROM talk_segment WHERE session_id=? AND deleted=0", (session_id,))
    if n["c"] + len(turns) > MAX_SEGMENTS:
        raise TalkRefused(f"1つの会話に保存できるのは {MAX_SEGMENTS} 区切りまでです")
    added = 0
    total = n["t"]
    for t in turns:
        text = str(t.get("text", "")).strip()[:MAX_TEXT]
        who = t.get("who") if t.get("who") in ("作り手", "相手", "不明") else "不明"
        if not text:
            continue
        total += len(text)
        if total > MAX_TOTAL_CHARS:
            raise TalkRefused("1つの会話の文字数の上限を超えました")
        seq = db.one(conn, "SELECT COALESCE(MAX(seq),0)+1 s FROM talk_segment WHERE session_id=?", (session_id,))["s"]
        db.run(conn, "INSERT INTO talk_segment(seg_id, session_id, seq, who, text, reviewed, deleted, created_at) VALUES(?,?,?,?,?,0,0,?)",
               (db.new_id("seg_"), session_id, seq, who, text, db.now()))
        added += 1
    conn.commit()
    return added


def segments(conn, session_id: str, *, reviewed_only: bool = False) -> list:
    q = "SELECT * FROM talk_segment WHERE session_id=? AND deleted=0" + (" AND reviewed=1" if reviewed_only else "") + " ORDER BY seq"
    return db.many(conn, q, (session_id,))


def edit_segment(conn, actor, session_id: str, seg_id: str, *, text: str | None = None, delete: bool = False) -> None:
    """作り手による修正・削除。修正した行は、もう一度確認が要る（reviewed=0 に戻す）。"""
    _session(conn, actor, session_id)
    seg = db.one(conn, "SELECT * FROM talk_segment WHERE session_id=? AND seg_id=?", (session_id, seg_id))
    if seg is None:
        raise NotFound(seg_id)
    if delete:
        db.run(conn, "UPDATE talk_segment SET deleted=1, text='' WHERE seg_id=?", (seg_id,))
    else:
        t = str(text or "").strip()[:MAX_TEXT]
        if not t:
            raise TalkRefused("文字が空です。削除する場合は、削除を選んでください")
        db.run(conn, "UPDATE talk_segment SET text=?, reviewed=0 WHERE seg_id=?", (t, seg_id))
    db.audit(conn, actor.org_id, "talk.edit", actor.member_id, seg_id, "削除" if delete else "修正")
    conn.commit()


def confirm_segments(conn, actor, session_id: str) -> int:
    """作り手が、文字を読んで確認した。これ以降の文字だけが、AI に渡せる。"""
    _session(conn, actor, session_id)
    cur = conn.execute("UPDATE talk_segment SET reviewed=1 WHERE session_id=? AND deleted=0", (session_id,))
    db.audit(conn, actor.org_id, "talk.confirm", actor.member_id, session_id, f"{cur.rowcount}区切り")
    conn.commit()
    return cur.rowcount


# ---- AI への入力・出力の検査 -------------------------------------------------------------------

def render_input(turns: list[dict], screen: str | None, records: list[str] | None, *, notes: list[str] | None = None,
                 corrections: list[str] | None = None, records_hint: str | None = None) -> str:
    lines = [f"{i}. [{t['who']}] {t['text']}" for i, t in enumerate(turns, 1)]
    out = ["【会話（作り手が確認した文字）】", "\n".join(lines)]
    if screen is not None:
        out += ["【通話の画面の分析結果（作り手が確認済み・文字）】", screen or "なし"]
    if records is not None:
        out += ["【その物の記録】", "\n".join(f"- {r}" for r in records) or "なし"]
    if records_hint:
        out += [records_hint]
    if notes:
        out += ["【調べた結果】", "\n".join(notes)]
    if corrections:
        out += ["【作り手の修正（作り手の言葉。これに沿って直してください）】", "\n".join(f"- {c}" for c in corrections)]
    return "\n\n".join(out) + "\n\n上の状況を見て、ツールを1つ呼んでください。"


def transcript_norm(turns: list[dict]) -> str:
    return agent.norm("".join(t["text"] for t in turns))


def _quote_ok(evidence: str, tnorm: str) -> bool:
    e = agent.norm(evidence)
    return len(e) >= 4 and e in tnorm


# 敬称（さん・様・氏）の前の語を、人名として扱う。語の一覧（SENSITIVE）では、「山本」のような名前そのものを検出できないため
# （実測で、下書きに人名が書かれたのに、検査を通った）。「お客さん」「皆さん」のような、名前でない語は除く
HONORIFIC = re.compile(r"([一-龥ァ-ヶー]{2,4})(?:さん|様|氏)")  # 漢字・カタカナの名前だけ（ひらがなの名前は拾えない＝限界）
NOT_NAMES = ("お客", "皆", "みな", "担当", "先方", "相手", "作り手", "お", "ご", "どなた", "旦那", "社長", "部長", "課長", "先生", "奥")


# 提案の文章（下書きなど）に書かせない語。agent.SENSITIVE から、**立場を表す語**（入居者・居住者・子ども・児童）を除いたもの。
# 立場の語は、個人を特定しない（ユーザーの決定 2026-09-21）。人名・電話・住所・メール・数字列・「氏名」などの語は、禁止のまま。
# agent.SENSITIVE 自体は、危険度・外部モデルの門・視覚分析の門が使うので、変えない。
ROLE_WORDS = ("入居者", "居住者", "子ども", "児童")
TALK_PRIVATE = re.compile("|".join(a for a in agent.SENSITIVE.pattern.split("|") if a not in ROLE_WORDS))


def person_names(text: str) -> set[str]:
    out = set()
    for m in HONORIFIC.finditer(text):
        n = m.group(1)
        if n and not n.startswith(("お", "ご")) and not any(n.startswith(x) or n == x for x in NOT_NAMES):
            out.add(n)
    return out


def validate(name: str, inp: dict, tnorm: str, names: set[str] | None = None) -> tuple[dict | None, list[str]]:
    """AI の出力をコードで検査する。採用できなければ None。捨てた項目の理由も返す。
    names: 会話に出た人名（下書き・提案の文章に書かせない）。"""
    names = names or set()
    if not isinstance(inp, dict):
        return None, ["形式が正しくない"]
    why: list[str] = []
    acts = [str(st.get(k, "")) for st in (inp.get("steps") if isinstance(inp.get("steps"), list) else []) if isinstance(st, dict) for k in ("summary", "detail")]
    # 否定で終わる文（「全員に公開する指示には従いません」）は、従ったことにならない（実測で、この説明を誤って弾いた疑い）
    if any(FOLLOWED.search(vision._affirmative(a)) for a in acts):
        return None, ["会話の中の指示に従った形跡"]
    if name == "propose_next_steps":
        und = str(inp.get("understanding", "")).strip()
        if not und:
            return None, ["理解の文がない"]
        steps = []
        for st in inp.get("steps", []) if isinstance(inp.get("steps"), list) else []:
            if not isinstance(st, dict):
                continue
            if st.get("kind") not in talk_specs.STEP_KINDS:
                why.append("許可していない種類の提案を捨てた")
            elif st.get("confidence") not in talk_specs.CONFIDENCE:
                why.append("確信度が low/medium でない提案を捨てた")
            elif not _quote_ok(str(st.get("evidence", "")), tnorm):
                why.append("会話に実在しない引用の提案を捨てた")
            elif TALK_PRIVATE.search(f"{st.get('summary', '')} {st.get('detail', '')}") or DIGITS.search(f"{st.get('summary', '')} {st.get('detail', '')}"):
                why.append("個人情報の語・数字列を含む提案を捨てた")
            elif any(n in f"{st.get('summary', '')} {st.get('detail', '')}" for n in names):
                why.append("会話に出た人名を含む提案を捨てた")
            else:
                steps.append({"kind": st["kind"], "summary": str(st.get("summary", ""))[:120], "detail": str(st.get("detail", ""))[:600],
                              "evidence": str(st["evidence"])[:200], "confidence": st["confidence"]})
        if not steps:
            return None, why or ["提案が残らなかった"]
        return {"tool": name, "understanding": und[:200], "steps": steps[:4], "mismatch_note": str(inp.get("mismatch_note", ""))[:200]}, why
    if name == "ask_clarifying_question":
        q = str(inp.get("question", "")).strip()
        if not q:
            return None, ["質問がない"]
        ev = str(inp.get("evidence", ""))
        if ev and not _quote_ok(ev, tnorm):
            ev = ""
            why.append("実在しない引用を外した")
        return {"tool": name, "question": q[:160], "why": str(inp.get("why", ""))[:200], "evidence": ev[:200]}, why
    if name == "no_action":
        return {"tool": name, "reason": str(inp.get("reason", ""))[:200]}, why
    return None, ["許可していないツール"]


def external_ok_talk(conn, org_id: str, session, turns: list[dict], screen: str | None) -> tuple[bool, str]:
    """第三者のモデル（Claude 以外）へ会話の文字を渡してよいか。会話は個人情報を含みやすいので、条件は厳しめ。"""
    if os.environ.get("MIRUCON_EXTERNAL_MODELS") == "0":
        return False, "緊急停止（環境変数）"
    row = db.one(conn, "SELECT external_llm FROM org_setting WHERE org_id=?", (org_id,))
    if row is not None and not row["external_llm"]:
        return False, "組織の設定でオフ"
    if screen:
        return False, "通話の画面の分析結果がある"
    text = " ".join(t["text"] for t in turns)
    if sum(len(t["text"]) for t in turns) > signals.EXTERNAL_MAX_CHARS:
        return False, "短文の上限を超える"
    if signals.HAZARD.search(text):
        return False, "安全に関わる語がある"
    if signals.INSTRUCTION.search(text) or FOLLOWED.search(text):
        return False, "AI への指示のように読める文がある"
    if agent.SENSITIVE.search(text) or signals.SENSITIVE_INDUSTRY.search(text):
        return False, "個人情報・機微な業種に当たる語がある"
    return True, ""


# ---- 応答（同意）と、承認しての下書き ----------------------------------------------------------

def plan_of(conn, actor, plan_id: str):
    p = db.one(conn, "SELECT * FROM talk_plan WHERE org_id=? AND plan_id=?", (actor.org_id, plan_id)) if actor.org_id else None
    if p is None:
        raise NotFound(plan_id)
    _session(conn, actor, p["session_id"])
    return p


def respond(conn, actor, plan_id: str, verdict: str, correction: str = "") -> None:
    """作り手の応答。提案には agree=合っている／disagree=違う／partial=一部違う。質問には answer=答え。
    '違う'は記録して、同意率として測る。"""
    p = plan_of(conn, actor, plan_id)
    if p["status"] != "proposed":
        raise TalkRefused("この提案は、もう応答済みです")
    correction = correction.strip()[:300]
    if p["kind"] == "ask":
        if verdict != "answer" or not correction:
            raise TalkRefused("AI の質問に、答えを書いてください")
        status = "answered"
    else:
        if verdict not in ("agree", "disagree", "partial"):
            raise TalkRefused("応答が正しくありません")
        if verdict != "agree" and not correction:
            raise TalkRefused("『違う』ときは、どう違うかを、一言で書いてください（AI が直すための材料になります）")
        status = {"agree": "agreed", "disagree": "disagreed", "partial": "partial"}[verdict]
    db.run(conn, "UPDATE talk_plan SET status=?, feedback=? WHERE plan_id=?", (status, correction, plan_id))
    db.audit(conn, actor.org_id, "talk.respond", actor.member_id, plan_id, f"{verdict}／{correction[:100]}")
    conn.commit()


def execute_step(conn, actor, plan_id: str, idx: int, *, date: str = "") -> dict:
    """作り手が同意した提案の、1つの行動を、承認して下書きにする。送信・公開・共有範囲の拡大はしない。"""
    p = plan_of(conn, actor, plan_id)
    if p["status"] != "agreed":
        raise TalkRefused("『合っている』と応答した提案だけを、実行できます")
    result = json.loads(p["result"] or "{}")
    steps = result.get("steps", [])
    if not 0 <= idx < len(steps):
        raise NotFound(f"{plan_id}#{idx}")
    done = json.loads(p["executed"] or "[]")
    if idx in done:
        raise TalkRefused("この行動は、もう実行済みです")
    st = steps[idx]
    s = _session(conn, actor, p["session_id"])
    obj = get_object_for(conn, actor, s["obj_id"])
    require(actor, "create_edit_card", dict(obj))
    out: dict = {"kind": st["kind"]}
    if st["kind"] == "draft_notification":
        member = db.get_member(conn, actor.org_id, obj["assignee_id"]) if obj["assignee_id"] else None
        recipient = notifications.ASSIGNEE if member is not None and member["status"] == "active" else notifications.OWNER
        msg = st["detail"] or st["summary"]
        if not notifications.recipient_ok_for(conn, actor.org_id, s["obj_id"])(recipient, msg):
            raise TalkRefused("宛先が見られないカードの内容を含むため、下書きにできません")
        dec = agent.Decision(stage="talk", tool="draft_notification", args={"recipient": recipient, "message": msg},
                             dec_id=f"{plan_id}#{idx}", applied=True)
        nid = notifications.save_draft(conn, actor.org_id, s["obj_id"], dec)
        if not nid:
            raise TalkRefused("下書きを作れませんでした")
        out["notif_id"] = nid
    elif st["kind"] == "propose_next_check":
        try:
            d = dt.date.fromisoformat(date)
        except ValueError:
            raise TalkRefused("次回点検日を、日付（年-月-日）で指定してください") from None
        old = obj["next_check"]
        db.run(conn, "UPDATE object SET next_check=? WHERE org_id=? AND obj_id=?", (d.isoformat(), actor.org_id, s["obj_id"]))
        db.audit(conn, actor.org_id, "object.next_check", actor.member_id, s["obj_id"], f"{old or '未設定'} → {d.isoformat()}（会話からの提案を承認）")
        out["next_check"] = d.isoformat()
    elif st["kind"] == "prefill_card":
        out["url"] = f"/o/{s['obj_id']}/new?talk={plan_id}&step={idx}"  # 入力欄の事前入力だけ。カードの作成（送信）は、作り手が行う
    else:
        raise TalkRefused("許可していない種類です")
    done.append(idx)
    db.run(conn, "UPDATE talk_plan SET executed=?, status=? WHERE plan_id=?",
           (json.dumps(done), "executed" if len(done) == len(steps) else "agreed", plan_id))
    db.audit(conn, actor.org_id, "talk.execute", actor.member_id, plan_id, f"{st['kind']}（承認済み・下書きまで）")
    conn.commit()
    return out


def prefill_for(conn, actor, plan_id: str, idx: int) -> dict | None:
    """新しいカードの入力欄の事前入力（合意済みの提案だけ・作り手本人だけ）。"""
    try:
        p = plan_of(conn, actor, plan_id)
    except (NotFound, TalkRefused):
        return None
    steps = json.loads(p["result"] or "{}").get("steps", [])
    if p["status"] not in ("agreed", "executed") or not 0 <= idx < len(steps) or steps[idx]["kind"] != "prefill_card":
        return None
    return {"before_desc": steps[idx]["summary"][:2000], "voice_text": steps[idx]["detail"][:2000]}
