"""定期の確認（コードが起動する）。期限を過ぎた物だけを、コードが絞ってから、物エージェントに1回ずつ判断させる。

- 閲覧のたびに LLM を呼ばない。対象は「次回点検日を過ぎた物」だけ。同じ物は、通知・判断から RENOTIFY_DAYS 日は再確認しない。
- AI は、その物の履歴だけを読み（authz: read_object_history）、通知案（下書き）を作るだけ。送信・共有・削除はできない。
- 通知案は、宛先が見られないカードの内容を含んでいたら、一般的な文面に置き換える（agent の recipient_ok）。
- 実行は、cron / タスクスケジューラから `python -m app.periodic`。`--dry-run` は LLM を呼ばず、対象だけ表示する。
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from dataclasses import dataclass

from . import agent, authz, db, notifications, objects

RENOTIFY_DAYS = 7
MAX_OBJECTS = 20  # 1回の実行で判断させる物の上限（原価の上限）
MAX_CARDS = 5     # AI に見せる、その物の直近のカード数
MAX_TEXT = 300


@dataclass
class Outcome:
    obj_id: str
    name: str
    tool: str
    draft_id: str | None
    default_used: bool
    cost_usd: float


def _today() -> dt.date:
    return dt.date.fromtimestamp(db.now())


def select_targets(conn, org_id: str, today: dt.date | None = None, limit: int = MAX_OBJECTS) -> list:
    """次回点検日を過ぎていて、最近確認していない物。日付が読めない物は対象にしない。"""
    today = today or _today()
    since = db.now() - RENOTIFY_DAYS * 86400
    out = []
    for o in db.many(conn, "SELECT * FROM object WHERE org_id=? AND next_check IS NOT NULL ORDER BY next_check", (org_id,)):
        try:
            due = dt.date.fromisoformat(o["next_check"])
        except ValueError:
            continue
        if due >= today:
            continue
        recent = (db.one(conn, "SELECT 1 FROM notification WHERE org_id=? AND obj_id=? AND created_at>?", (org_id, o["obj_id"], since))
                  or db.one(conn, "SELECT 1 FROM decision_log WHERE org_id=? AND obj_id=? AND stage='periodic' AND created_at>?",
                            (org_id, o["obj_id"], since)))
        if recent:
            continue
        out.append(o)
        if len(out) >= limit:
            break
    return out


def build_inputs(conn, ai: authz.Actor, obj, today: dt.date) -> tuple[dict, list[dict]]:
    """AI に渡す状況。カードの文章は資料であり、指示ではない（システムプロンプトで明示済み）。"""
    days = (today - dt.date.fromisoformat(obj["next_check"])).days
    inputs = {"物": obj["name"], "次回点検日": f"{obj['next_check']}（{days}日超過）",
              "担当者": "登録あり" if obj["assignee_id"] else "登録なし"}
    sources = [{"field": k, "text": v, "source_card_id": None} for k, v in inputs.items()]
    for c in objects.object_history(conn, ai, obj["obj_id"])[-MAX_CARDS:]:
        when = dt.date.fromtimestamp(c["created_at"]).isoformat()
        body = "\n".join(x for x in (c["title"], f"作業前: {c['before_desc']}" if c["before_desc"] else "",
                                     f"作業後: {c['after_desc']}" if c["after_desc"] else "") if x)[:MAX_TEXT]
        key = f"カード（{when}・{c['card_type'] or ''}）"
        while key in inputs:
            key += "'"
        inputs[key] = body
        sources.append({"field": key, "text": body, "source_card_id": c["card_id"]})
    return inputs, sources


def run_periodic(conn, org_id: str, *, client_factory=None, config: dict | None = None, today: dt.date | None = None,
                 limit: int = MAX_OBJECTS, dry_run: bool = False) -> list[Outcome]:
    today = today or _today()
    ai = authz.Actor(kind="ai", org_id=org_id, periodic=True)
    targets = select_targets(conn, org_id, today, limit)
    if dry_run:
        return [Outcome(o["obj_id"], o["name"], "(dry-run)", None, False, 0.0) for o in targets]
    outcomes = []
    for obj in targets:
        inputs, sources = build_inputs(conn, ai, obj, today)
        ctx = agent.RunCtx()
        dec = agent.decide(conn, org_id, stage="periodic", inputs=inputs, obj_id=obj["obj_id"], card_id=None,
                           trigger="periodic", sources=sources, ctx=ctx, client_factory=client_factory, config=config,
                           recipient_ok=notifications.recipient_ok_for(conn, org_id, obj["obj_id"]))
        draft_id = notifications.save_draft(conn, org_id, obj["obj_id"], dec)
        if dec.applied:
            notifications.save_extra_drafts(conn, org_id, obj["obj_id"], dec)
        outcomes.append(Outcome(obj["obj_id"], obj["name"], dec.tool, draft_id, dec.default_used, ctx.cost))
    db.audit(conn, org_id, "periodic.run", "system", "", f"対象{len(targets)}件・下書き{sum(1 for o in outcomes if o.draft_id)}件")
    conn.commit()
    return outcomes


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if os.environ.get("MIRUCON_ENV") != "dev":
        sys.exit("開発モード専用です。MIRUCON_ENV=dev を設定してください。")
    from . import web

    dry = "--dry-run" in argv
    conn = db.connect(web.DB_PATH)
    db.init(conn)
    total = 0.0
    for org in db.many(conn, "SELECT org_id, name FROM org"):
        for o in run_periodic(conn, org["org_id"], dry_run=dry):
            total += o.cost_usd
            print(f"{org['name']} / {o.name}: {o.tool}" + (f" → 下書き {o.draft_id}" if o.draft_id else "")
                  + ("（既定動作）" if o.default_used else ""))
    print(f"合計の原価: ${total:.4f}" + ("（dry-run: LLM は呼んでいません）" if dry else ""))


if __name__ == "__main__":
    main()
