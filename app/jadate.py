"""日本語の相対的な日付の言い方（来週の火曜日・来月の第2週・明日 など）を、今日の日付から、日付の候補にする。

- **決定的な処理**（AI は使わない）。AI は日付の言い方までしか書けない（今日の日付を知らない）ので、承認のとき作り手が日付を入れていた
  （実測: 提案された行動の 51% が、この日付の入力待ち）。ここでは**候補を作って見せる**だけで、確定は作り手（`talk.execute_step` は ISO の日付だけを受け取る）。
- 曖昧な言い方は、候補を 1〜3 件にして、ラベルに前提を書く（週の始まりは月曜。「第N週」は、その月の最初の月曜から数える）。
"""

from __future__ import annotations

import calendar
import datetime as dt
import re

WEEKDAYS = "月火水木金土日"
MAX_CANDIDATES = 3
_WD = f"[{WEEKDAYS}]"
_num = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}


def _monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _label(d: dt.date, why: str) -> dict:
    return {"date": d.isoformat(), "label": f"{why}（{d.month}/{d.day}・{WEEKDAYS[d.weekday()]}）"}


def _month_add(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return dt.date(y, m + 1, 1)


def _n(s: str) -> int:
    return _num.get(s) or int(s)


def candidates(text: str, today: dt.date) -> list[dict]:
    """text に含まれる日付の言い方を、日付の候補（{"date": ISO, "label": ...}）にする。見つからなければ空。重複は除く。"""
    t = re.sub(r"\s+", "", str(text or ""))
    out: list[dict] = []

    def add(d: dt.date, why: str):
        if d >= today and all(c["date"] != d.isoformat() for c in out):
            out.append(_label(d, why))

    for m in re.finditer(r"(再来週|来週|今週)の?(" + _WD + r")曜?日?", t):
        base = _monday(today) + dt.timedelta(weeks={"再来週": 2, "来週": 1, "今週": 0}[m.group(1)])
        add(base + dt.timedelta(days=WEEKDAYS.index(m.group(2))), m.group(0))
    for m in re.finditer(r"(再来週|来週)(?!の?" + _WD + ")", t):
        add(_monday(today) + dt.timedelta(weeks=2 if m.group(1) == "再来週" else 1), m.group(1) + "の月曜（週の始まり）")
    for m in re.finditer(r"来月の?第?([1-5一二三四五])週", t):
        first = _month_add(today, 1)
        mon = first + dt.timedelta(days=(7 - first.weekday()) % 7)  # その月の最初の月曜
        add(mon + dt.timedelta(weeks=_n(m.group(1)) - 1), f"来月の第{m.group(1)}週の月曜")
    if re.search(r"来月(?!の?第?[1-5一二三四五]週)", t):
        add(_month_add(today, 1), "来月の初め")
    if re.search(r"今月末|月末", t):
        d = dt.date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
        add(d, "今月末")
    for word, n in (("明後日", 2), ("明日", 1), ("本日", 0), ("今日", 0)):
        if word in t:
            add(today + dt.timedelta(days=n), word)
    for m in re.finditer(r"(\d{1,2})日後", t):
        add(today + dt.timedelta(days=int(m.group(1))), m.group(0))
    for m in re.finditer(r"(\d{1,2})週間後", t):
        add(today + dt.timedelta(weeks=int(m.group(1))), m.group(0))
    for m in re.finditer(r"(\d{1,2})[かヶか]月後", t):
        first = _month_add(today, int(m.group(1)))
        add(dt.date(first.year, first.month, min(today.day, calendar.monthrange(first.year, first.month)[1])), m.group(0))
    for m in re.finditer(r"(\d{1,2})月(\d{1,2})日", t):
        try:
            d = dt.date(today.year, int(m.group(1)), int(m.group(2)))
            add(d if d >= today else dt.date(today.year + 1, d.month, d.day), m.group(0))
        except ValueError:
            pass
    if "今週末" in t:
        add(_monday(today) + dt.timedelta(days=5), "今週末の土曜")
    # 曜日だけ（「火曜日にお願いします」）: 次にその曜日になる日（今日を含まない）。上の「来週の…」で拾ったものは、重複を除く
    if not out:
        for m in re.finditer(r"(" + _WD + r")曜日?", t):
            d = today + dt.timedelta(days=1)
            while d.weekday() != WEEKDAYS.index(m.group(1)):
                d += dt.timedelta(days=1)
            add(d, m.group(0) + "（次の）")
    return out[:MAX_CANDIDATES]
