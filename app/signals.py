"""コードだけで決まる見立て。LLM を呼ばない。

役割は3つ。

1. 確認事項の検知（notes）: 矛盾・繰り返す故障・期限超過・停滞・記録欠損・要約とのずれ・作業前後のずれ・
   資料内の命令文の候補を、規則で見つける。**「異常確定」とは書かない。**人が確かめるための提示にとどめる。
2. 危険度の分類（risk）: low / medium / high。曖昧な判定だけをモデルへ渡すための、前段の仕分け。
3. 必要の部屋（room）: その場面で意味のないツール・危険なツールを、段階ごとの一覧から**外す**。増やさない。

位置づけ: 語句の一致による規則。一次の防御は「そのツールを AI に渡さないこと」（agent.SPEC["forbidden"]）で、
ここは二次の「気づいて人に見せる」層。
"""

from __future__ import annotations

import datetime as dt
import json
import re

from . import db

DAY = 86400.0
REPEAT_DAYS = 30      # この日数のうちに故障・交換が2件以上なら、繰り返しとして挙げる
STALE_DAYS = 90       # 最新の記録がこれより古ければ、停滞・情報が古いとして挙げる
MIN_RECORDS = 2       # 要約・将来の案の根拠にしたい、日付のある記録の最小件数
ELIGIBLE_SCOPES = ("link_30d", "org_only")  # 要約の根拠と同じ。招待限定は混ぜない

CALL_SCREEN_KEY = "写真の出どころ"  # cards._run_agent が、通話の画面から取り込んだ写真があるときに、判断の入力へ足す項目

NORMAL = re.compile(r"正常|異常なし|問題なし|良好")
ABNORMAL = re.compile(r"異常|不良|故障|漏れ|異音|破損|劣化")
FAILURE = re.compile(r"故障|再発|また.{0,4}(壊|故障)|交換した|取り替え")
# 資料の中の「命令文」の候補。AI への指示のように読める書き方を拾う（従わないことは agent 側で担保済み）
INSTRUCTION = re.compile(
    r"(これまで|以前|上記|前)の(指示|命令|ルール)を?(無視|忘れ)"
    r"|指示を?(無視|忘れ)"
    r"|あなたは\s*AI"
    r"|(全員|社外|外部)に(公開|共有|送信)し"
    r"|(ぼかし|マスク|モザイク)を?(外|解除|削除)"
    r"|(システム|AI|アシスタント)へ?の指示"
    r"|(必ず|今すぐ).{0,12}(してください|しなさい|せよ)")
# 作業前の「対象」を表す語。作業後にこの語がなければ、前後がかみ合っていない疑いとして挙げる。
# 語の一覧で拾う（表記の揺れや、ここにない部材は拾えない。限定的な規則）
TARGET = re.compile(r"フィルター|配管|ドア|扉|照明|ランプ|ポンプ|ベルト|パッキン|バルブ|タイル|壁紙|天井|窓|"
                    r"ファン|モーター|電池|バッテリー|蛇口|便器|手すり|車止め|ブレーカー|受水槽|非常灯|シャンプー台")


def _day(ts: float) -> str:
    return dt.date.fromtimestamp(ts).isoformat()


def _text(c) -> str:
    return " ".join(str(c[k] or "") for k in ("title", "before_desc", "after_desc", "voice_text"))


def usable_cards(cards) -> list:
    """確認事項・確信度の材料にしてよいカード（要約の根拠と同じ範囲。公開範囲を混ぜない）。"""
    return [c for c in cards if c["scope"] in ELIGIBLE_SCOPES]


def _note(rule: str, level: str, message: str, ids) -> dict:
    return {"rule": rule, "level": level, "message": message, "evidence_ids": list(ids)}


def notes(conn, org_id: str, obj_id: str, *, now: float | None = None) -> list[dict]:
    """物の確認事項。各件に、検出した規則の名前と、元になった記録の ID を必ず付ける。"""
    now = db.now() if now is None else now
    obj = db.get_object(conn, org_id, obj_id)
    if obj is None:
        return []
    cards = usable_cards(db.cards_of_object(conn, org_id, obj_id))
    out: list[dict] = []

    normal = [c for c in cards if NORMAL.search(_text(c))]
    abnormal = [c for c in cards if ABNORMAL.search(_text(c))]
    if normal and abnormal:
        out.append(_note("contradiction", "high", "同じ物に「正常」と「異常」の記録があります。測定した時刻と条件を要確認。",
                         [c["card_id"] for c in normal + abnormal]))

    recent_failures = [c for c in cards if FAILURE.search(_text(c)) and now - c["created_at"] <= REPEAT_DAYS * DAY]
    if len(recent_failures) >= 2:
        out.append(_note("repeated_failure", "warning",
                         f"{REPEAT_DAYS}日以内に、故障・交換の記録が{len(recent_failures)}件あります。同じ原因かどうかを要確認。",
                         [c["card_id"] for c in recent_failures]))

    if obj["next_check"]:
        try:
            over = (dt.date.fromtimestamp(now) - dt.date.fromisoformat(obj["next_check"])).days
            if over > 0:
                out.append(_note("overdue_check", "warning", f"次回点検日（{obj['next_check']}）を{over}日過ぎています。実施の有無を要確認。", []))
        except ValueError:
            out.append(_note("overdue_check", "warning", "次回点検日の形式を読み取れません。要確認。", []))

    if cards:
        newest = max(cards, key=lambda c: c["created_at"])
        idle = int((now - newest["created_at"]) / DAY)
        if idle >= STALE_DAYS:
            out.append(_note("stalled", "warning", f"最新の記録から{idle}日たっています。今の状態を要確認。", [newest["card_id"]]))

    if len(cards) < MIN_RECORDS:
        out.append(_note("missing_record", "warning",
                         f"根拠にできる記録が{len(cards)}件しかありません（{MIN_RECORDS}件以上あると、経緯をたどれます）。要確認。",
                         [c["card_id"] for c in cards]))

    if obj["summary"] and obj["summary_status"] in ("current", "held") and cards:
        try:
            cited = set(json.loads(obj["summary_sources"] or "[]"))
        except json.JSONDecodeError:
            cited = set()
        newest = max(cards, key=lambda c: c["created_at"])
        if cited and newest["card_id"] not in cited:
            out.append(_note("summary_drift", "warning", "今の要約は、最新の記録を根拠にしていません。要約の更新を要確認。",
                             [newest["card_id"]]))

    for c in cards:
        before, after = str(c["before_desc"] or ""), " ".join(str(c[k] or "") for k in ("after_desc", "voice_text", "title"))
        targets = {m.group(0) for m in TARGET.finditer(before)}
        if before and after and targets and not any(t in after for t in targets):
            out.append(_note("before_after_mismatch", "warning",
                             f"作業前の「{'・'.join(sorted(targets))}」が、作業後の説明に出てきません。取り違えがないか要確認。",
                             [c["card_id"]]))

    # 通話の画面から取り込んだ写真。招待限定のカード（＝通話の画面は必ずそうなる）も対象にするので、
    # 表示する側（web）が、そのカードを見られる人にだけ出す（存在を、見られない人に漏らさない）
    call_cards = [r["card_id"] for r in db.many(
        conn, "SELECT DISTINCT i.card_id FROM image i JOIN card c ON c.card_id=i.card_id "
              "WHERE c.org_id=? AND c.obj_id=? AND c.deleted_at IS NULL AND i.source='call_screen'", (org_id, obj_id))]
    if call_cards:
        out.append(_note("call_screen_card", "warning",
                         "通話の画面から取り込んだ写真があります。通話相手の同意と、相手の顔・名前が隠れているかを要確認。", call_cards))

    hit = [c["card_id"] for c in cards if INSTRUCTION.search(_text(c))]
    if hit:
        out.append(_note("injection", "high",
                         "資料の中に、AI への指示のように読める文があります。AI は従いませんが、内容を要確認。"
                         "（語句による限定的な検知で、完全ではありません）", hit))
    return out


# 第三者の提供元のモデルへ渡さない業種の語（実写真の検証で、医療の場面が SENSITIVE をすり抜けたため）。
# 外部モデルの条件にだけ使う。危険度や共有の判断（agent.SENSITIVE）は変えない。
SENSITIVE_INDUSTRY = re.compile(r"医療|病院|医院|手術|患者|看護|医師|診療|診察|入院|カルテ|薬剤|介護|利用者|要介護|施設入所|検体|レントゲン")
SENSITIVE_NOTES = {"repeated_failure", "overdue_check", "stalled", "summary_drift", "before_after_mismatch", "missing_record"}
# 人や設備の安全に関わる語。これがあるときは、AI に案を出させず、人が現場を確認する
HAZARD = re.compile(r"感電|漏電|ガス漏れ|火災|発火|煙が出|転落|墜落|挟まれ|負傷|けが|怪我|死亡|救急|中毒|窒息|倒壊|崩落|爆発|有毒|石綿|アスベスト")


def risk(inputs: dict, note_list: list[dict]) -> tuple[str, str]:
    """危険度と、その理由。

    high は「安全に関わるので、AI に案を出させない」合図。**資料内の命令文は high にしない**。
    命令文があっても、本来の仕事（種類の判断・期限超過の通知案など）は続けるのが正しい振る舞いで、
    従わないことは agent 側（プロンプトとツールの絞り）で担保している。ここでは警告として人に見せる。
    """
    from . import agent  # 循環参照を避けるため、ここで読み込む（SENSITIVE の正本は agent 側）

    text = " ".join(str(v) for v in inputs.values())
    rules_seen = {n["rule"] for n in note_list}
    if HAZARD.search(text):
        return "high", "安全に関わる語があります。AI は案を出さず、人が現場を確認してください。"
    if INSTRUCTION.search(text) or "injection" in rules_seen:
        return "medium", "資料の中に、AI への指示のように読める文があります。AI は従いませんが、内容の確認が要ります。"
    if CALL_SCREEN_KEY in inputs:
        return "medium", "ビデオ通話の画面から取り込んだ写真があります。通話相手など、第三者が写っている可能性があります。"
    if "contradiction" in rules_seen:
        return "medium", "記録に「正常」と「異常」が同居しています。人の確認が要ります。"
    if agent.SENSITIVE.search(text):
        return "medium", "個人情報・機密に当たる語があります。慎重に判断します。"
    if rules_seen & SENSITIVE_NOTES:
        return "medium", "確認事項（" + "・".join(sorted(rules_seen & SENSITIVE_NOTES)) + "）があります。"
    return "low", "危険を示す合図は見つかりませんでした。"


# 安全に関わるときに外すツール。X10 のツールは元々「提案」と「下書き」だけで、適用も送信も人が行う。
# 危ないのは、状態を断定して記録を書き換える update_summary だけ。通知の下書きは「人に知らせる」側なので、
# 安全に関わるときこそ残す（外すと、危険を伝える手段を AI から取り上げることになる）。
ASSERTIVE_TOOLS = {"update_summary"}


# 絞り込みの規則。既定は、_guard がすでに拒否しているもの（scope・question）と、安全に関わるもの（hazard）だけ。
# images と records は、判断の内容を実際に変えるので、実 API で一致率を確かめてから既定に入れる。
DEFAULT_RULES = ("scope", "question", "hazard")
ALL_RULES = ("scope", "question", "hazard", "images", "records")


def room(stage: str, tool_names: list[str], *, risk: str = "low", current_scope: str = "org_only",
         has_images: bool = True, questions_used: int = 0, eligible_cards: int | None = None,
         rules: tuple | list | None = None, call_screen: bool = False, injection: bool = False) -> dict:
    """この場面で渡してよいツールだけに絞る。**減らすだけで、増やさない。**

    rules: 使う規則。既定は DEFAULT_RULES。
    戻り値の reasons は、何をなぜ外したかの説明。判断ログと物の作業室に出す。
    """
    use = set(DEFAULT_RULES if rules is None else rules)
    from . import authz

    tools, reasons = list(tool_names), []

    def drop(name: str, why: str) -> None:
        if name in tools:
            tools.remove(name)
            reasons.append(f"{name}: {why}")

    if risk == "high" and "hazard" in use:
        for t in sorted(ASSERTIVE_TOOLS):
            drop(t, "安全に関わる語があるため、状態を断定して記録を書き換えることはしない（人の確認が先）")
    if "scope" in use and (current_scope not in authz.SCOPES
                           or not any(authz.scope_is_narrower(s, current_scope) for s in authz.SCOPES)):
        drop("propose_narrower_scope", "今が最も狭い共有範囲で、これ以上狭められない")
    if "images" in use and not has_images and not call_screen:  # 通話の画面は、追加のぼかしの提案を必ず残す
        drop("propose_extra_mask", "このカードに画像がない")
    if "question" in use and questions_used >= 1:
        drop("request_more_input", "質問は1回の起動につき1回まで、すでに使った")
    if "records" in use and eligible_cards is not None and eligible_cards < 2:
        drop("draft_notification", "根拠にできる記録が2件未満で、矛盾を比べられない")
    if not tools:  # 読取りの選択肢は必ず残す（判断そのものを止めない）
        tools = ["no_action"]
        reasons.append("no_action: 渡せるツールが残らなかったため、何もしない選択肢だけを残した")
    # 安全に関わるとき・通話の画面の共有判断（第三者が写る）は、弱いモデルに任せず、最上位から
    # 通話の画面で指示が混ざっているときは、どの段でも最上位から（実測: Sonnet は 20〜26 秒考え込んだが、Opus は 9 秒）
    strong = risk == "high" or (call_screen and (stage == "privacy" or injection))
    if call_screen and (stage == "privacy" or injection):
        reasons.append("first_kind: 通話の画面（第三者が写りうる）の" + ("指示の混入" if injection and stage != "privacy" else "共有判断") + "なので、最上位のモデルから始める")
    return {"tools": tools, "first_kind": "decide_heavy" if strong else None, "reasons": reasons}


def confidence(cards, *, now: float | None = None) -> dict:
    """根拠の量と鮮度。**故障の確率でも、AI の確信でもない。**将来の案を出してよいかの足切りに使う。"""
    now = db.now() if now is None else now
    usable = usable_cards(cards)
    latest = max((c["created_at"] for c in usable), default=None)
    fresh = int((now - latest) / DAY) if latest else None
    ok = len(usable) >= MIN_RECORDS and fresh is not None and fresh < STALE_DAYS
    return {"dated": len(usable), "latest_date": _day(latest) if latest else None, "fresh_days": fresh,
            "label": "usable" if ok else "insufficient",
            "explanation": "記録の件数と新しさだけを見た指標です。故障の確率でも、AI の確信度でもありません。"}


# ---- 第三者の提供元（Claude 以外の外部モデル）へ、文章を渡してよいか ------------------------------------------
EXTERNAL_MAX_CHARS = 600   # 「小さい」の定義。長い文章は、提供元へ渡さない
EXTERNAL_STAGES = ("classify",)  # 出力が最小（type_id 1個）で、間違えても作り手が直せる判断だけ


def external_ok(conn, org_id: str, stage: str, inputs: dict, *, current_scope: str, notes: list | None = None,
                cfg: dict | None = None) -> tuple[bool, str]:
    """Claude 以外の外部モデルへ、この入力を渡してよいか。**コードだけで決める。1つでも外れたら False（理由つき）。**

    語句による限定的な判定で、個人情報の完全な検出ではない。だから範囲を「分類だけ・600文字以下」に狭めている。
    """
    import os

    if os.environ.get("MIRUCON_EXTERNAL_MODELS") == "0":
        return False, "緊急停止（環境変数）"
    stages = tuple((cfg or {}).get("external_stages") or EXTERNAL_STAGES)
    if stage not in stages:
        return False, f"{stage} は対象外の判断"
    org = db.one(conn, "SELECT external_llm FROM org_setting WHERE org_id=?", (org_id,))
    if org is not None and not org["external_llm"]:
        return False, "組織の設定でオフ"
    if current_scope == "invited_only":
        return False, "共有範囲が招待した人のみ"
    if CALL_SCREEN_KEY in inputs:
        return False, "通話の画面から取り込んだ写真がある"
    text = " ".join(str(v) for v in inputs.values())
    if sum(len(str(v)) for v in inputs.values()) > EXTERNAL_MAX_CHARS:  # 項目の文字数の合計（つなぎの空白は数えない）
        return False, f"{EXTERNAL_MAX_CHARS}文字を超える"
    # 個人情報・安全・指示・矛盾に関わるものだけで止める。記録の少なさ・停滞・期限などの確認事項は、
    # 個人情報とは無関係なので、外部モデルを止める理由にしない（止めると、新しい物の最初のカードで、永久に使われない）
    from . import agent  # 循環参照を避ける（SENSITIVE の正本は agent 側）

    if HAZARD.search(text):
        return False, "安全に関わる語がある"
    if INSTRUCTION.search(text) or any(n["rule"] == "injection" for n in (notes or [])):
        return False, "AI への指示のように読める文がある"
    if agent.SENSITIVE.search(text):
        return False, "個人情報・機密に当たる語がある"
    if SENSITIVE_INDUSTRY.search(text):
        return False, "医療・介護など、機微な業種の語がある"
    if any(n["rule"] == "contradiction" for n in (notes or [])):
        return False, "記録に矛盾がある"
    return True, ""
