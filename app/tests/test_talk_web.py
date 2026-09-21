"""会話から次を考える機能の、画面・設定・音声分析（第2の経路）。"""

import json
from types import SimpleNamespace

import pytest

from app import auth, cards, db, objects, talk, talk_audio, web
from app.tests.fakes import client_dynamic
from app.tests.test_talk import C, QUOTE, TURNS, enable, make_card, propose, step
from app.tests.test_web import HOST, get, login, post
from app.tests.test_web_upload import multipart

WAV = b"RIFF" + b"\x00" * 4000


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    web.reset_limits()
    auth.reset_rate()
    for k in ("MIRUCON_TALK", "MIRUCON_TALK_AUDIO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(web, "CLIENT_FACTORY", None)
    yield


@pytest.fixture
def obj(conn, alice):
    return objects.register_object(conn, alice, "空調")[0]


def start_session(conn, cookie, obj, source="typed"):
    r = post(conn, f'/o/{obj["obj_id"]}/talk/start', {"source": source, "consent": "1"}, cookie)
    assert r.status == 303
    return dict(r.headers)["Location"].split("?s=")[1]


# ---- 設定 ---------------------------------------------------------------------------------------

def test_only_the_owner_can_switch_the_talk_settings_and_it_is_audited(conn, owner, alice):
    assert post(conn, "/settings/talk", {"enabled": "1"}, login(conn, alice)).status in (403, 404)
    assert not talk.enabled(conn, "org_1")
    assert post(conn, "/settings/talk", {"enabled": "1"}, login(conn, owner)).status == 303
    assert talk.enabled(conn, "org_1") and db.one(conn, "SELECT 1 FROM audit_log WHERE action='org.talk_llm'")


def test_the_audio_setting_needs_the_talk_setting_and_goes_off_with_it(conn, owner):
    ck = login(conn, owner)
    post(conn, "/settings/talk-audio", {"enabled": "1"}, ck)
    assert not talk.audio_enabled(conn, "org_1")  # 会話の機能がオフなので、オンにならない
    post(conn, "/settings/talk", {"enabled": "1"}, ck)
    post(conn, "/settings/talk-audio", {"enabled": "1"}, ck)
    assert talk.audio_enabled(conn, "org_1")
    post(conn, "/settings/talk", {"enabled": "0"}, ck)
    assert not talk.audio_enabled(conn, "org_1")


def test_the_settings_do_not_disturb_the_other_org_settings(conn, owner):
    ck = login(conn, owner)
    post(conn, "/settings/external-llm", {"enabled": "0"}, ck)
    post(conn, "/settings/vision-ai", {"enabled": "1"}, ck)
    post(conn, "/settings/talk", {"enabled": "1"}, ck)
    r = db.one(conn, "SELECT external_llm, vision_llm, talk_llm FROM org_setting WHERE org_id='org_1'")
    assert (r["external_llm"], r["vision_llm"], r["talk_llm"]) == (0, 1, 1)


def test_the_home_page_says_what_is_sent_and_kept(conn, owner):
    body = get(conn, "/", login(conn, owner)).body.decode()
    assert "会話から次の業務を考える機能" in body and "音声そのものは、保存せず" in body and "第三者の提供元へ渡ります" in body and "7日" in body


# ---- 入口・ページ ---------------------------------------------------------------------------------

def test_the_link_and_page_exist_only_when_enabled(conn, alice, obj):
    ck = login(conn, alice)
    assert "会話から次の業務を考える" not in get(conn, f'/o/{obj["obj_id"]}', ck).body.decode()
    assert get(conn, f'/o/{obj["obj_id"]}/talk', ck).status == 403
    enable(conn)
    assert "会話から次の業務を考える" in get(conn, f'/o/{obj["obj_id"]}', ck).body.decode()
    assert get(conn, f'/o/{obj["obj_id"]}/talk', ck).status == 200


def test_starting_needs_the_consent_checkbox(conn, alice, obj):
    enable(conn)
    r = post(conn, f'/o/{obj["obj_id"]}/talk/start', {"source": "typed"}, login(conn, alice))
    assert r.status == 400 and "伝えたか" in r.body.decode()


def test_the_audio_analysis_choice_is_offered_only_when_enabled(conn, alice, obj):
    enable(conn)
    ck = login(conn, alice)
    assert 'value="audio_analysis"' not in get(conn, f'/o/{obj["obj_id"]}/talk', ck).body.decode()
    enable(conn, audio=1)
    assert 'value="audio_analysis"' in get(conn, f'/o/{obj["obj_id"]}/talk', ck).body.decode()


def test_the_microphone_is_allowed_only_on_a_capturing_talk_page(conn, alice, obj):
    enable(conn)
    ck = login(conn, alice)

    def pp(path):
        return dict(get(conn, path, ck).all_headers())["Permissions-Policy"]

    assert "microphone=()" in pp(f'/o/{obj["obj_id"]}/talk')                     # 入口のページは、閉じたまま
    assert "microphone=()" in pp(f'/o/{obj["obj_id"]}')                          # 既存のページは、変わらない
    assert "microphone=()" in pp(f'/o/{obj["obj_id"]}/new')
    typed = start_session(conn, ck, obj, "typed")
    assert "microphone=()" in pp(f'/o/{obj["obj_id"]}/talk?s={typed}')           # 手入力は、マイクを使わない
    mic = start_session(conn, ck, obj, "mic")
    p = pp(f'/o/{obj["obj_id"]}/talk?s={mic}')
    assert "microphone=(self)" in p and "camera=()" in p and "geolocation=()" in p


def test_the_capture_script_is_served_and_fails_closed_without_local_recognition():
    js = (web.STATIC_DIR / "talk.js").read_text(encoding="utf-8")
    assert 'processLocally: true' in js and "音声を外へ送る認識には切り替えません" in js
    assert 'a !== "available"' in js and "SR.available" in js  # 端末内で使えると確認できなければ、始めない


def test_only_the_known_static_files_are_served(conn):
    assert get(conn, "/static/talk.js").status == 200
    assert get(conn, "/static/other.js").status == 404


# ---- 画面からの一連の流れ -------------------------------------------------------------------------

def test_the_whole_flow_over_http_ends_at_a_draft_that_is_not_sent(conn, alice, obj, monkeypatch):
    enable(conn)
    ck = login(conn, alice)
    monkeypatch.setattr(web, "CLIENT_FACTORY", client_dynamic(lambda k, i: propose(), echo_model=True))
    sid = start_session(conn, ck, obj)
    for t in TURNS:
        assert post(conn, f"/talk/{sid}/add", {"who": t["who"], "text": t["text"]}, ck).status == 303
    page = get(conn, f'/o/{obj["obj_id"]}/talk?s={sid}', ck).body.decode()
    assert "未確認" in page and "確認するまで、AI には渡りません" in page
    r = post(conn, f"/talk/{sid}/analyze", {"mode": "single"}, ck)
    assert r.status == 400 and "確認" in r.body.decode()                        # 確認前は、AI に渡らない
    assert post(conn, f"/talk/{sid}/confirm", {}, ck).status == 303
    assert post(conn, f"/talk/{sid}/analyze", {"mode": "single"}, ck).status == 303
    page = get(conn, f'/o/{obj["obj_id"]}/talk?s={sid}', ck).body.decode()
    assert "私はこう理解しました" in page and QUOTE in page and "合っている" in page
    pid = db.one(conn, "SELECT plan_id FROM talk_plan")["plan_id"]
    assert post(conn, f"/talk/plan/{pid}/execute/0", {}, ck).status == 400        # 同意の前は、実行できない
    assert post(conn, f"/talk/plan/{pid}/respond", {"verdict": "agree"}, ck).status == 303
    r = post(conn, f"/talk/plan/{pid}/execute/0", {}, ck)
    assert r.status == 303 and dict(r.headers)["Location"] == "/n"
    n = db.one(conn, "SELECT status FROM notification")
    assert n["status"] == "draft"


def test_a_stranger_cannot_open_or_operate_a_session(conn, alice, bob, obj):
    enable(conn)
    sid = start_session(conn, login(conn, alice), obj)
    assert get(conn, f'/o/{obj["obj_id"]}/talk?s={sid}', login(conn, bob)).status == 404
    assert post(conn, f"/talk/{sid}/add", {"who": "相手", "text": "x"}, login(conn, bob)).status == 400


def test_text_from_the_conversation_is_escaped_on_the_page(conn, alice, obj):
    enable(conn)
    ck = login(conn, alice)
    sid = start_session(conn, ck, obj)
    post(conn, f"/talk/{sid}/add", {"who": "相手", "text": '<script>alert(1)</script>"><img src=x onerror=1>'}, ck)
    body = get(conn, f'/o/{obj["obj_id"]}/talk?s={sid}', ck).body.decode()
    assert "<script>alert(1)" not in body and "&lt;script&gt;" in body


def test_disagreeing_over_http_needs_a_reason_and_can_be_revised(conn, alice, obj, monkeypatch):
    enable(conn)
    ck = login(conn, alice)
    monkeypatch.setattr(web, "CLIENT_FACTORY", client_dynamic(lambda k, i: propose(), echo_model=True))
    sid = start_session(conn, ck, obj)
    post(conn, f"/talk/{sid}/add", {"who": "作り手", "text": TURNS[1]["text"]}, ck)
    post(conn, f"/talk/{sid}/confirm", {}, ck)
    post(conn, f"/talk/{sid}/analyze", {"mode": "single"}, ck)
    pid = db.one(conn, "SELECT plan_id FROM talk_plan")["plan_id"]
    assert post(conn, f"/talk/plan/{pid}/respond", {"verdict": "disagree"}, ck).status == 400
    assert post(conn, f"/talk/plan/{pid}/respond", {"verdict": "disagree", "correction": "見積もりの連絡です"}, ck).status == 303
    assert "考え直してもらう" in get(conn, f'/o/{obj["obj_id"]}/talk?s={sid}', ck).body.decode()
    assert post(conn, f"/talk/plan/{pid}/revise", {}, ck).status == 303
    assert db.one(conn, "SELECT COUNT(*) c FROM talk_plan")["c"] == 2


# ---- カード作成の事前入力 --------------------------------------------------------------------------

def test_the_new_card_form_is_prefilled_only_after_agreement_and_only_for_the_creator(conn, alice, bob, obj, monkeypatch):
    enable(conn)
    ck = login(conn, alice)
    monkeypatch.setattr(web, "CLIENT_FACTORY", client_dynamic(
        lambda k, i: propose(step(kind="prefill_card", summary="フィルター<b>清掃</b>の記録", detail="風が弱い")), echo_model=True))
    sid = start_session(conn, ck, obj)
    post(conn, f"/talk/{sid}/add", {"who": "作り手", "text": TURNS[1]["text"]}, ck)
    post(conn, f"/talk/{sid}/confirm", {}, ck)
    post(conn, f"/talk/{sid}/analyze", {"mode": "single"}, ck)
    pid = db.one(conn, "SELECT plan_id FROM talk_plan")["plan_id"]
    url = f'/o/{obj["obj_id"]}/new?talk={pid}&step=0'
    assert "清掃" not in get(conn, url, ck).body.decode()                       # 同意の前は、空のまま
    post(conn, f"/talk/plan/{pid}/respond", {"verdict": "agree"}, ck)
    body = get(conn, url, ck).body.decode()
    assert "&lt;b&gt;清掃&lt;/b&gt;" in body and "<b>清掃</b>" not in body
    assert "清掃" not in get(conn, url, login(conn, bob)).body.decode()
    assert get(conn, f'/o/{obj["obj_id"]}/new?talk=zzz&step=x', ck).status == 200  # 壊れた指定でも、通常のフォーム


# ---- 音声分析（第2の経路）--------------------------------------------------------------------------

def audio_factory(result=None, seen=None):
    """呼ばれた経路の名前と、送った内容を記録する偽のクライアント。"""
    result = result or {"turns": [{"who": "相手", "text": "来週でお願いします"}, {"who": "作り手", "text": "分かりました"}]}
    calls = []

    def create(**kw):
        calls.append(kw)
        tu = SimpleNamespace(type="tool_use", name="extract_talk", input=result)
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        raw = SimpleNamespace(headers={"x-orca-resolved-model": kw["model"].split("/")[-1]},
                              parse=lambda: SimpleNamespace(content=[tu], usage=usage))
        return raw

    client = SimpleNamespace(messages=SimpleNamespace(with_raw_response=SimpleNamespace(create=create)))
    routes = []

    def factory(p):
        routes.append(p)
        return client

    factory.calls, factory.routes = calls, routes
    return factory


def audio_session(conn, alice, obj):
    enable(conn, audio=1)
    return talk.create_session(conn, alice, obj["obj_id"], source="audio_analysis", consent=True)


def test_audio_analysis_is_off_by_default_and_cannot_be_started(conn, alice, obj):
    enable(conn)
    with pytest.raises(talk.TalkRefused, match="音声分析"):
        talk.create_session(conn, alice, obj["obj_id"], source="audio_analysis", consent=True)


def test_the_audio_goes_only_to_orcas_openai_route_and_only_as_an_input_audio_block(conn, alice, obj):
    sid = audio_session(conn, alice, obj)
    f = audio_factory()
    n = talk_audio.analyze(conn, alice, sid, WAV, "wav", client_factory=f)
    assert n == 2 and set(f.routes) == {"orca-openai"}                          # Claude 直へは行かない
    blocks = f.calls[0]["messages"][0]["content"]
    assert any(b["type"] == "input_audio" and b["input_audio"]["format"] == "wav" for b in blocks)
    assert f.calls[0]["model"] == "google/gemini-3.5-flash-lite"                # 安くて十分な第1（実測）


def test_the_result_is_saved_as_unreviewed_text_and_the_audio_is_not_stored(conn, alice, obj, tmp_path, monkeypatch):
    sid = audio_session(conn, alice, obj)
    talk_audio.analyze(conn, alice, sid, WAV, "wav", client_factory=audio_factory())
    segs = talk.segments(conn, sid)
    assert [g["reviewed"] for g in segs] == [0, 0]                              # 作り手の確認が要る
    dump = " ".join(str(dict(r)) for t in ("talk_segment", "talk_session", "audit_log") for r in conn.execute(f"SELECT * FROM {t}"))
    assert "RIFF" not in dump and "input_audio" not in dump                     # 音声は、どこにも残らない


def test_a_failed_first_model_escalates_to_the_next_audio_model(conn, alice, obj):
    sid = audio_session(conn, alice, obj)
    seq = [{"turns": []}, {"turns": [{"who": "相手", "text": "聞き取れました"}]}]
    calls = []

    def factory(p):
        f = audio_factory(seq[min(len(calls), 1)])
        calls.append(1)
        return f(p)

    assert talk_audio.analyze(conn, alice, sid, WAV, "wav", client_factory=factory) == 1
    assert len(calls) == 2


@pytest.mark.parametrize("audio,fmt,why", [(b"", "wav", "長すぎ"), (b"x" * (talk_audio.MAX_BYTES + 1), "wav", "長すぎ"), (WAV, "webm", "形式")],
                         ids=["empty", "too_long", "bad_format"])
def test_the_audio_chunk_limits(conn, alice, obj, audio, fmt, why):
    sid = audio_session(conn, alice, obj)
    f = audio_factory()
    with pytest.raises(talk.TalkRefused, match=why):
        talk_audio.analyze(conn, alice, sid, audio, fmt, client_factory=f)
    assert not f.calls


def test_the_number_of_audio_chunks_is_limited(conn, alice, obj):
    sid = audio_session(conn, alice, obj)
    db.run(conn, "UPDATE talk_session SET audio_chunks=?", (talk_audio.MAX_CHUNKS,))
    conn.commit()
    with pytest.raises(talk.TalkRefused, match="区切りまで"):
        talk_audio.analyze(conn, alice, sid, WAV, "wav", client_factory=audio_factory())


def test_a_session_that_is_not_audio_analysis_cannot_send_audio(conn, alice, obj):
    enable(conn, audio=1)
    sid = talk.create_session(conn, alice, obj["obj_id"], source="typed", consent=True)
    f = audio_factory()
    with pytest.raises(talk.TalkRefused, match="音声分析ではありません"):
        talk_audio.analyze(conn, alice, sid, WAV, "wav", client_factory=f)
    assert not f.calls


def test_audio_is_refused_when_the_object_has_a_recent_call_screen_photo(conn, alice, obj, jpeg):
    fn = lambda k, i: [("select_card_type", {**C, "type_id": "maintenance"})] if "select_card_type" in {t["name"] for t in k["tools"]} \
        else [("no_action", C)] if "propose_extra_mask" in {t["name"] for t in k["tools"]} \
        else [("write_card_text", {"title": "t", "changes": ["c"], "description": "d"})]
    cards.create_card(conn, alice, obj["obj_id"], before_desc="点検", images_in=[("before", jpeg, None, "call_screen")], mask_confirmed=True,
                      client_factory=client_dynamic(fn, echo_model=True))
    sid = audio_session(conn, alice, obj)
    f = audio_factory()
    with pytest.raises(talk.TalkRefused, match="通話の画面"):
        talk_audio.analyze(conn, alice, sid, WAV, "wav", client_factory=f)
    assert not f.calls


def test_the_emergency_stop_blocks_audio_even_when_enabled(conn, alice, obj, monkeypatch):
    sid = audio_session(conn, alice, obj)
    monkeypatch.setenv("MIRUCON_TALK_AUDIO", "0")
    with pytest.raises(talk.TalkRefused, match="オフ"):
        talk_audio.analyze(conn, alice, sid, WAV, "wav", client_factory=audio_factory())


def test_the_audio_upload_route_checks_the_gates_before_anything_leaves(conn, alice, obj, monkeypatch):
    sid = audio_session(conn, alice, obj)
    f = audio_factory()
    monkeypatch.setattr(web, "CLIENT_FACTORY", f)
    ck = login(conn, alice)
    body, ctype = multipart({"format": "wav"}, [("audio", WAV)])
    h = {**HOST, "cookie": ck, "content-type": ctype}
    assert web.handle(conn, "POST", f"/talk/{sid}/audio", h, body).status == 200
    assert f.calls and talk.segments(conn, sid)
    body, ctype = multipart({"format": "webm"}, [("audio", WAV)])
    n = len(f.calls)
    r = web.handle(conn, "POST", f"/talk/{sid}/audio", {**HOST, "cookie": ck, "content-type": ctype}, body)
    assert r.status == 400 and len(f.calls) == n


def test_the_audio_route_allows_a_larger_body_than_other_posts_but_not_a_huge_one():
    assert web.body_limit("POST", "/talk/tks_x/audio", {"content-type": "multipart/form-data; boundary=x"}) == web.MAX_UPLOAD
    assert web.body_limit("POST", "/talk/tks_x/add", {"content-type": "multipart/form-data; boundary=x"}) == web.MAX_BODY


def test_the_audio_page_tells_the_creator_that_the_voice_goes_to_a_third_party(conn, alice, obj):
    sid = audio_session(conn, alice, obj)
    body = get(conn, f'/o/{obj["obj_id"]}/talk?s={sid}', login(conn, alice)).body.decode()
    assert "第三者の提供元へ送って" in body and "保存はしません" in body


def test_audio_analysis_text_still_goes_through_the_creators_review(conn, alice, obj):
    from app import jobs, talk_loop
    sid = audio_session(conn, alice, obj)
    talk_audio.analyze(conn, alice, sid, WAV, "wav", client_factory=audio_factory())
    f = client_dynamic(lambda k, i: propose(), echo_model=True)
    with pytest.raises(talk.TalkRefused, match="確認"):
        talk_loop.start(conn, alice, sid, jobs=jobs.Inline(), client_factory=f)
    assert not f.state["calls"]
