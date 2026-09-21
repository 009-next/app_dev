"""改善案（A26〜A32・面積の門・操作モード）のテスト。既存のテストは変えず、ここに足す。"""

import datetime as dt
import hashlib
import io
import re
import struct
import wave

import pytest
from PIL import Image

from app import agent, auth, cards, db, jadate, objects, talk, talk_audio, vision, web
from app.tests.fakes import client_dynamic
from app.tests.test_talk import TN, enable as enable_talk, propose, step
from app.tests.test_talk_web import audio_factory, audio_session
from app.tests.test_vision import GOOD, enable, screen, vfactory
from app.tests.test_web import get, login, post

C = {"reason": "r", "evidence": ""}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    web.reset_limits()
    auth.reset_rate()
    for k in ("MIRUCON_VISION", "MIRUCON_TALK", "MIRUCON_TALK_AUDIO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(web, "CLIENT_FACTORY", None)
    yield


@pytest.fixture
def obj(conn, alice):
    return objects.register_object(conn, alice, "空調")[0]


# ---- A30: 立場の語は許し、人名・連絡先・数字は禁止のまま --------------------------------------------

@pytest.mark.parametrize("detail", ["入居者の水漏れの修理の連絡です", "利用者への案内の下書き", "居住者向けの点検のお知らせ", "子どもの安全のための点検"])
def test_role_words_are_allowed_in_a_draft(detail):
    assert talk.validate("propose_next_steps", propose(step(detail=detail))[0][1], TN)[0] is not None


@pytest.mark.parametrize("detail", ["入居者の氏名を伝える", "入居者の電話番号を伝える", "住所は東京都です", "入居者へ a@example.test で連絡", "口座番号を伝える", "個人情報を含む図面"])
def test_identifying_words_are_still_refused_in_a_draft(detail):
    assert talk.validate("propose_next_steps", propose(step(detail=detail))[0][1], TN)[0] is None


def test_a_role_word_with_a_name_from_the_conversation_is_still_refused():
    assert talk.validate("propose_next_steps", propose(step(detail="入居者の山本様への連絡"))[0][1], TN, names={"山本"})[0] is None


def test_the_shared_sensitive_list_is_unchanged():
    """視覚分析・外部モデル・危険度の門は、これまでどおり「入居者」で止まる（会話の下書きだけを分けた）。"""
    for w in talk.ROLE_WORDS:
        assert agent.SENSITIVE.search(w)
    assert not any(talk.TALK_PRIVATE.search(w) for w in talk.ROLE_WORDS)


# ---- A26: 聞き取りにくい音声は、上のモデルへ ------------------------------------------------------

def wav_bytes(seconds: float, rate=16000) -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * int(rate * seconds))
    return b.getvalue()


def seq_factory(results):
    """呼び出しごとに、順に別の結果を返す（使ったモデル名も記録する）。"""
    from types import SimpleNamespace as N
    calls = []

    def create(**kw):
        calls.append(kw["model"])
        inp = results[min(len(calls) - 1, len(results) - 1)]
        tu = N(type="tool_use", name="extract_talk", input=inp)
        usage = N(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return N(headers={"x-orca-resolved-model": kw["model"].split("/")[-1]}, parse=lambda: N(content=[tu], usage=usage))

    client = N(messages=N(with_raw_response=N(create=create)))
    f = lambda p: client  # noqa: E731
    f.calls = calls
    return f


OK_TURNS = {"turns": [{"who": "相手", "text": "来週の火曜日にもう一度点検に伺います"}]}


def test_an_unclear_answer_goes_to_the_next_audio_model(conn, alice, obj):
    sid = audio_session(conn, alice, obj)
    f = seq_factory([{**OK_TURNS, "unclear": True}, OK_TURNS])
    talk_audio.analyze(conn, alice, sid, wav_bytes(6), "wav", client_factory=f)
    assert f.calls == ["google/gemini-3.5-flash-lite", "google/gemini-3.5-flash"]
    assert "聞き取りにくい" in db.one(conn, "SELECT detail FROM audit_log WHERE action='talk.audio'")["detail"]


def test_too_few_characters_for_the_length_goes_up(conn, alice, obj):
    sid = audio_session(conn, alice, obj)
    f = seq_factory([{"turns": [{"who": "相手", "text": "はい"}]}, OK_TURNS])
    talk_audio.analyze(conn, alice, sid, wav_bytes(10), "wav", client_factory=f)  # 10 秒で 2 文字
    assert len(f.calls) == 2


def test_a_clear_answer_does_not_add_calls(conn, alice, obj):
    sid = audio_session(conn, alice, obj)
    f = seq_factory([{"turns": [{"who": "相手", "text": "来週の火曜日にもう一度点検に伺います。日程はあとで連絡します"}]}])
    talk_audio.analyze(conn, alice, sid, wav_bytes(6), "wav", client_factory=f)
    assert len(f.calls) == 1


def test_short_clips_and_other_formats_do_not_use_the_ratio_rule():
    assert talk_audio.escalation_reason(None, [{"text": "はい"}], 2.0) == ""          # 3 秒未満は判定しない
    assert talk_audio.escalation_reason(None, [{"text": "はい"}], None) == ""         # 長さが分からない（mp3 など）


def test_the_escalation_can_be_switched_off(conn, alice, obj):
    from app import llm
    sid = audio_session(conn, alice, obj)
    f = seq_factory([{**OK_TURNS, "unclear": True}, OK_TURNS])
    talk_audio.analyze(conn, alice, sid, wav_bytes(6), "wav", client_factory=f, config={**llm.load_config(), "talk_audio_escalate": False})
    assert len(f.calls) == 1


def test_if_the_upper_model_fails_the_first_result_is_kept(conn, alice, obj):
    sid = audio_session(conn, alice, obj)
    f = seq_factory([{**OK_TURNS, "unclear": True}, {"turns": []}, {"turns": []}])
    assert talk_audio.analyze(conn, alice, sid, wav_bytes(6), "wav", client_factory=f) == 1  # 空の答えで、聞こえた分を捨てない


def test_the_last_tier_result_is_used_even_if_still_unclear(conn, alice, obj):
    sid = audio_session(conn, alice, obj)
    f = seq_factory([{**OK_TURNS, "unclear": True}])
    assert talk_audio.analyze(conn, alice, sid, wav_bytes(6), "wav", client_factory=f) == 1
    assert len(f.calls) == 3


# ---- A32: 日付の候補 ----------------------------------------------------------------------------

TODAY = dt.date(2026, 9, 21)  # 月曜


@pytest.mark.parametrize("text,expected", [
    ("来週の火曜日", "2026-09-29"), ("来週の火曜日にもう一度点検", "2026-09-29"), ("再来週の金曜", "2026-10-09"), ("今週の水曜日", "2026-09-23"),
    ("来月の第2週", "2026-10-12"), ("来月の第1週", "2026-10-05"), ("明日", "2026-09-22"), ("明後日", "2026-09-23"), ("3日後", "2026-09-24"),
    ("2週間後", "2026-10-05"), ("10月5日", "2026-10-05"), ("今週末", "2026-09-26"), ("月末までに", "2026-09-30"), ("来週", "2026-09-28"),
    ("来月", "2026-10-01"), ("火曜日にお願いします", "2026-09-22"), ("1月10日", "2027-01-10"),
])
def test_relative_dates_become_candidates(text, expected):
    assert jadate.candidates(text, TODAY)[0]["date"] == expected


def test_nothing_is_invented_when_there_is_no_date_phrase():
    assert jadate.candidates("フィルターを清掃した", TODAY) == []
    assert jadate.candidates("", TODAY) == [] and jadate.candidates(None, TODAY) == []


def test_candidates_are_never_in_the_past_unique_and_at_most_three():
    c = jadate.candidates("明日と明日、来週の火曜日、来週の水曜日、来週の木曜日、来週の金曜日", TODAY)
    assert len(c) <= 3 and len({x["date"] for x in c}) == len(c) and all(x["date"] >= TODAY.isoformat() for x in c)


def test_an_invalid_calendar_date_is_ignored():
    assert jadate.candidates("2月30日に伺います", TODAY) == []


def test_the_label_states_the_assumption():
    assert "月曜" in jadate.candidates("来月の第2週", TODAY)[0]["label"] and "週の始まり" in jadate.candidates("来週", TODAY)[0]["label"]


def _agreed_next_check_plan(conn, alice, obj, monkeypatch, detail="来週の火曜日"):
    from app.tests.test_talk import run, session
    enable_talk(conn)
    sid = session(conn, alice, obj)
    pid, _ = run(conn, alice, sid, lambda k, i: propose(step(kind="propose_next_check", summary="次回点検日を登録", detail=detail)), mode="single")
    talk.respond(conn, alice, pid, "agree")
    return pid


def test_the_page_offers_date_candidates_but_the_server_still_needs_an_iso_date(conn, alice, obj, monkeypatch):
    pid = _agreed_next_check_plan(conn, alice, obj, monkeypatch)
    ck = login(conn, alice)
    sid = db.one(conn, "SELECT session_id FROM talk_plan WHERE plan_id=?", (pid,))["session_id"]
    body = get(conn, f'/o/{obj["obj_id"]}/talk?s={sid}', ck).body.decode()
    assert "日付の候補" in body and "で承認して登録する" in body and re.search(r'name="date" value="20\d\d-\d\d-\d\d"', body)
    with pytest.raises(talk.TalkRefused, match="日付"):
        talk.execute_step(conn, alice, pid, 0, date="来週の火曜日")  # 言い方のままは受け取らない（既存の約束）


def test_choosing_a_candidate_registers_that_date(conn, alice, obj, monkeypatch):
    pid = _agreed_next_check_plan(conn, alice, obj, monkeypatch)
    cand = jadate.candidates("来週の火曜日", dt.date.fromtimestamp(db.now()))[0]["date"]
    r = post(conn, f"/talk/plan/{pid}/execute/0", {"date": cand}, login(conn, alice))
    assert r.status == 303
    assert db.one(conn, "SELECT next_check FROM object WHERE obj_id=?", (obj["obj_id"],))["next_check"] == cand


# ---- A29: 機微な現場は、組織の設定で画像分析を止める -----------------------------------------------------

def _card(conn, alice, obj, img=None, ratio=None, source="call_screen"):
    fn = lambda k, i: [("select_card_type", {**C, "type_id": "maintenance"})] if "select_card_type" in {t["name"] for t in k["tools"]} \
        else [("no_action", C)] if "propose_extra_mask" in {t["name"] for t in k["tools"]} \
        else [("write_card_text", {"title": "t", "changes": ["c"], "description": "d"})]
    item = ("before", img or screen((300, 150, 100, 60)), None, source) + ((ratio,) if ratio is not None else ())
    card = cards.create_card(conn, alice, obj["obj_id"], before_desc="配管を点検", after_desc="継手を確認", images_in=[item], mask_confirmed=True,
                             client_factory=client_dynamic(fn, echo_model=True))["card"]
    return card, db.one(conn, "SELECT * FROM image WHERE card_id=?", (card["card_id"],))


def test_only_the_owner_can_declare_a_sensitive_industry_and_it_is_audited(conn, owner, alice):
    assert post(conn, "/settings/sensitive-industry", {"enabled": "1"}, login(conn, alice)).status in (403, 404)
    assert not vision.industry_blocked(conn, "org_1")
    assert post(conn, "/settings/sensitive-industry", {"enabled": "1"}, login(conn, owner)).status == 303
    assert vision.industry_blocked(conn, "org_1") and db.one(conn, "SELECT 1 FROM audit_log WHERE action='org.sensitive_industry'")


def test_a_sensitive_industry_organization_never_shows_images_to_the_ai(conn, alice, obj):
    enable(conn)
    card, image = _card(conn, alice, obj)
    db.run(conn, "UPDATE org_setting SET sensitive_industry=1 WHERE org_id='org_1'")
    conn.commit()
    f = vfactory()
    with pytest.raises(vision.VisionRefused, match="機微な現場"):
        vision.start(conn, alice, card["card_id"], image["image_id"], confirmed=True, jobs=web.JOBS, client_factory=f)
    assert not f.state["calls"]
    page = get(conn, f'/c/{card["card_id"]}', login(conn, alice)).body.decode()
    assert "機微な現場" in page and "AI に見せて、分析する" not in page


def test_the_industry_setting_does_not_disturb_the_others(conn, owner):
    ck = login(conn, owner)
    post(conn, "/settings/vision-ai", {"enabled": "1"}, ck)
    post(conn, "/settings/talk", {"enabled": "1"}, ck)
    post(conn, "/settings/sensitive-industry", {"enabled": "1"}, ck)
    r = db.one(conn, "SELECT vision_llm, talk_llm, sensitive_industry FROM org_setting WHERE org_id='org_1'")
    assert (r["vision_llm"], r["talk_llm"], r["sensitive_industry"]) == (1, 1, 1)


# ---- 面積の門: 端末が測った割合（拡張モード）と、旧来 ------------------------------------------------

def degrade(b: bytes) -> bytes:
    im = Image.open(io.BytesIO(b)).convert("RGB")
    im = im.resize((im.width // 2, im.height // 2), Image.BILINEAR)
    o = io.BytesIO()
    im.save(o, "JPEG", quality=30)
    return o.getvalue()


def gentle_screen(open_rect=None, size=(1280, 720), seed=3) -> bytes:
    """本物のフィルターに近い合成画面: 短辺の 1/4 のブロックで、色の差はおだやか。開けた範囲は、細かい模様（ノイズ）。"""
    import random
    im = Image.new("RGB", size)
    rnd = random.Random(seed)
    for by in range(0, size[1], 180):
        for bx in range(0, size[0], 180):
            im.paste((rnd.randrange(90, 150), rnd.randrange(90, 150), rnd.randrange(90, 150)), (bx, by, bx + 180, by + 180))
    if open_rect:
        x, y, w, h = open_rect
        for j in range(h):
            for i in range(w):
                im.putpixel((x + i, y + j), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    o = io.BytesIO()
    im.save(o, "JPEG", quality=88)
    return o.getvalue()


def test_the_recorded_ratio_is_saved_only_for_call_screens_and_only_when_valid(conn, alice, obj):
    _, ok = _card(conn, alice, obj, ratio=0.04)
    assert ok["open_ratio"] == pytest.approx(0.04)
    _, cam = _card(conn, alice, obj, ratio=0.04, source="camera")
    assert cam["open_ratio"] is None
    for bad in (-0.1, 1.5, True, "0.1", float("nan")):
        _, i = _card(conn, alice, obj, ratio=bad)
        assert i["open_ratio"] is None, bad


def test_without_a_recorded_ratio_the_old_decision_is_unchanged(conn, alice, obj):
    """旧来モード・古い画像: 画素の見積もりだけで判定する（変えていない）。"""
    enable(conn)
    card, image = _card(conn, alice, obj)
    assert image["open_ratio"] is None
    vision.start(conn, alice, card["card_id"], image["image_id"], confirmed=True, jobs=web.JOBS, client_factory=vfactory())
    wide, wimg = _card(conn, alice, obj, img=screen((0, 0, 640, 200)))
    with pytest.raises(vision.VisionRefused, match="広すぎ"):
        vision.start(conn, alice, wide["card_id"], wimg["image_id"], confirmed=True, jobs=web.JOBS, client_factory=vfactory())


def test_the_legacy_estimate_is_exactly_what_it_was():
    """旧来の見積もり（open_ratio）の値を固定する。変えると、旧来モードの判定が変わる。"""
    assert vision.open_ratio(screen()) == pytest.approx(0.0170, abs=0.002)
    assert vision.open_ratio(screen((300, 150, 100, 60))) == pytest.approx(0.0545, abs=0.003)
    assert vision.open_ratio(screen((0, 0, 640, 200))) == pytest.approx(0.5966, abs=0.01)


def test_a_wide_recorded_ratio_is_refused_even_if_the_pixels_look_small(conn, alice, obj):
    enable(conn)
    card, image = _card(conn, alice, obj, ratio=0.35)
    with pytest.raises(vision.VisionRefused, match="広すぎ"):
        vision.start(conn, alice, card["card_id"], image["image_id"], confirmed=True, jobs=web.JOBS, client_factory=vfactory())


def test_a_recorded_ratio_that_disagrees_with_the_pixels_is_refused(conn, alice, obj):
    """端末の値を、そのままは信じない: 広く開けた画像に「5% だけ開けた」と申告しても拒否する。"""
    enable(conn)
    card, image = _card(conn, alice, obj, img=screen((0, 0, 640, 200)), ratio=0.05)
    f = vfactory()
    with pytest.raises(vision.VisionRefused, match="合いません"):
        vision.start(conn, alice, card["card_id"], image["image_id"], confirmed=True, jobs=web.JOBS, client_factory=f)
    assert not f.state["calls"]


def test_an_honest_small_ratio_passes_and_survives_compression(conn, alice, obj):
    """圧縮・低解像度で、画素の見積もりが狂っても、記録された割合で正しく通る。"""
    enable(conn)
    card, image = _card(conn, alice, obj, img=degrade(gentle_screen((300, 150, 200, 120))), ratio=200 * 120 / (1280 * 720))
    vision.start(conn, alice, card["card_id"], image["image_id"], confirmed=True, jobs=web.JOBS, client_factory=vfactory())
    assert vision.latest(conn, "org_1", card["card_id"], image["image_id"])["status"] == "done"


def test_the_strict_estimate_is_less_noisy_than_the_legacy_one_on_a_degraded_masked_screen():
    b = degrade(gentle_screen())
    assert vision.open_estimate_strict(b) <= vision.open_ratio(b)
    assert vision.open_estimate_strict(b) < 0.05


def test_the_legacy_estimate_drifts_up_under_compression_and_the_recorded_ratio_does_not(conn, alice, obj):
    """今回の改善の動機: 画素の見積もりは、圧縮で上がる（全面ぼかしでも）。記録された割合は、圧縮に影響されない。"""
    b = gentle_screen()
    assert vision.open_ratio(degrade(b)) > 5 * max(vision.open_ratio(b), 0.001)
    card, image = _card(conn, alice, obj, img=degrade(b), ratio=0.0)
    assert image["open_ratio"] == 0.0


def test_the_double_check_constants_come_from_the_sweep():
    assert (vision.DOUBLECHECK_FRAC, vision.DOUBLECHECK_MARGIN, vision.OPEN_MAX_RATIO) == (0.12, 0.09, 0.20)


def test_the_form_field_is_parsed_defensively(conn, alice):
    assert web._float_or_none("0.1234") == pytest.approx(0.1234)
    assert web._float_or_none("") is None and web._float_or_none(None) is None and web._float_or_none("abc") is None


def test_low_resolution_images_get_a_warning_but_are_not_refused(conn, alice, obj):
    small = screen(size=(640, 360))
    assert vision.image_warnings(small)
    big = Image.new("RGB", (1280, 720), (10, 10, 10))
    o = io.BytesIO()
    big.save(o, "JPEG")
    assert vision.image_warnings(o.getvalue()) == []
    enable(conn)
    card, image = _card(conn, alice, obj)
    assert "解像度" in get(conn, f'/c/{card["card_id"]}', login(conn, alice)).body.decode() or "小さく" in get(conn, f'/c/{card["card_id"]}', login(conn, alice)).body.decode()
    vision.start(conn, alice, card["card_id"], image["image_id"], confirmed=True, jobs=web.JOBS, client_factory=vfactory())  # 拒否しない


# ---- 端末の操作（mask.js）: モードの切り替えと、旧来の動作を守る -----------------------------------------

def mask_js():
    return (web.STATIC_DIR / "mask.js").read_text(encoding="utf-8")


def test_the_form_defaults_to_the_legacy_mode(conn, alice, obj):
    body = get(conn, f'/o/{obj["obj_id"]}/new', login(conn, alice)).body.decode()
    assert 'class="mask-mode-select"' in body and body.index('value="legacy"') < body.index('value="extended"')
    assert 'class="mask-tool-select"' in body and "なぞって囲む" in body and 'class="muted mask-area"' in body


def test_the_selects_are_not_named(conn, alice, obj):
    body = get(conn, f'/o/{obj["obj_id"]}/new', login(conn, alice)).body.decode()
    for cls in ("mask-mode-select", "mask-tool-select"):
        tag = re.search(r"<select[^>]*" + cls + r"[^>]*>", body).group(0)
        assert "name=" not in tag


def test_the_script_sends_the_area_only_in_the_extended_mode():
    js = mask_js()
    assert 'let mode = "legacy"' in js
    assert re.search(r"if \(w\.reveal && isExtended\(\)\) fd\.append\(\"open_ratio_\"", js)
    assert js.count("open_ratio_") == 1


def test_the_script_never_persists_the_mode():
    lines = [l for l in mask_js().splitlines() if not l.lstrip().startswith("//")]
    assert "localStorage" not in "\n".join(lines)


def test_the_pixelate_function_is_byte_for_byte_what_it_was():
    """JS と Python の一致（合成画像の SHA-256）は、この関数の計算に依存する。変えたら、一致を再確認する。"""
    js = mask_js()
    a = js.index("  function pixelate(ctx, r) {")
    b = js.index("  class Widget {")
    body = js[a:b].strip()
    assert hashlib.sha256(body.encode()).hexdigest()[:16] == PIXELATE_SHA


def test_a_traced_shape_is_cut_by_a_hard_threshold_not_a_soft_clip():
    """clip() は縁が半透明で、形の外の 1 画素に元の色が混ざる（実測）。しきい値で 0/1 に分ける。"""
    js = mask_js()
    assert "m[k + 3] >= 128" in js and "ctx.clip()" not in js and ".clip()" not in js


def test_the_area_is_measured_on_a_black_and_white_surface_and_blocks_over_limit():
    js = mask_js()
    assert "OPEN_LIMIT = 0.20" in js and "d[i] > 127" in js and "w.openRatio > OPEN_LIMIT" in js


PIXELATE_SHA = "3f2c6822ebf59af5"  # 2026-09-21 の pixelate の全文。ブラウザで、JS↔Python の SHA-256 一致（97x61・200x120）を確認した版
