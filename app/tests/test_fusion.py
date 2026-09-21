"""統合分析（音声×共有画面→資料・メール下書き。デモ向け・既定オフ）と、資料の生成・見た目の追加。"""

import io
import json
import os
import re
import urllib.parse
import zipfile
from xml.dom import minidom

import pytest
from PIL import Image

from app import auth, db, docgen, fusion, talk, theme, vision, web
from app.tests.fakes import client_dynamic
from app.tests.test_talk import C
from app.tests.test_vision import make_card, screen
from app.tests.test_web import get, login, post
from app.tests.test_web_upload import multipart

WAV = b"RIFF" + b"\x00" * 4000
TURNS = [{"who": "相手", "text": "この重機のバケットの周りを、来週の火曜日にもう一度点検してほしいです"},
         {"who": "作り手", "text": "分かりました。点検の結果を、表にまとめてメールでお送りします"}]
GOOD = {
    "understanding": "重機のバケット周りを、来週の火曜日に再点検し、結果を表で送る、と理解しました。",
    "targets": [{"label": "油圧ショベルのバケット", "box": [0.35, 0.4, 0.3, 0.3], "evidence": "この重機のバケットの周りを", "visible_basis": "黄色いアームの先の爪", "confidence": "medium"},
                {"label": "カラーコーン", "box": [0.7, 0.6, 0.1, 0.2], "evidence": "点検してほしいです", "visible_basis": "橙色の円錐", "confidence": "low"}],
    "table": {"title": "点検項目", "columns": ["項目", "状態"], "rows": [["バケット", "要確認"], ["ピン", "要確認"]]},
    "report": {"title": "点検の説明", "sections": [{"heading": "経緯", "paragraphs": ["来週の火曜日に、再点検を行う。"]}]},
    "email": {"subject": "再点検のご連絡", "body": "再点検の結果を、表にまとめてお送りします。"},
    "next_work": [{"label": "バケットの摩耗を確認する", "reason": "会話で、周りの点検が話題になった"}],
}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    web.reset_limits()
    auth.reset_rate()
    for k in ("MIRUCON_TALK", "MIRUCON_TALK_AUDIO", "MIRUCON_VISION", "MIRUCON_FUSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(web, "CLIENT_FACTORY", None)
    yield


def flags(conn, **kw):
    v = {"talk_llm": 1, "talk_audio": 1, "vision_llm": 1, "fusion_demo": 1, "sensitive_industry": 0, **kw}
    db.run(conn, "INSERT INTO org_setting(org_id, external_llm, talk_llm, talk_audio, vision_llm, fusion_demo, sensitive_industry) VALUES('org_1',1,?,?,?,?,?) "
                 "ON CONFLICT(org_id) DO UPDATE SET talk_llm=excluded.talk_llm, talk_audio=excluded.talk_audio, vision_llm=excluded.vision_llm, "
                 "fusion_demo=excluded.fusion_demo, sensitive_industry=excluded.sensitive_industry",
           (v["talk_llm"], v["talk_audio"], v["vision_llm"], v["fusion_demo"], v["sensitive_industry"]))
    conn.commit()


def factory(fuse=GOOD, turns=TURNS):
    def fn(k, i):
        names = {t["name"] for t in k["tools"]}
        if "extract_talk" in names:
            return [("extract_talk", {"turns": turns})]
        return [("fuse_and_draft", fuse)]
    return client_dynamic(fn, echo_model=True)


@pytest.fixture
def setup(conn, alice, obj_):
    flags(conn)
    card, image = make_card(conn, alice, obj_)
    return card, image


@pytest.fixture
def obj_(conn, alice):
    from app import objects
    return objects.register_object(conn, alice, "空調")[0]


def run(conn, alice, card, image, f=None, edited=None, audio=WAV, consent=True):
    f = f or factory()
    fid = fusion.start(conn, alice, card["card_id"], image["image_id"], audio, "wav", consent=consent, jobs=web.JOBS, client_factory=f)
    return fid, f


def finish(conn, alice, fid, f, edited=None):
    fusion.confirm_and_analyze(conn, alice, fid, edited, jobs=web.JOBS, client_factory=f)
    return fusion.get(conn, alice, fid)


# ---- 資料の生成（標準ライブラリだけ）-----------------------------------------------------------------

def test_xlsx_is_a_valid_zip_with_well_formed_xml_and_neutralizes_formulas_and_control_chars():
    b = docgen.make_xlsx("点検/表:1", ["項目", "状態"], [["=1+1", "ok & <b>"], ["\x00\x07柱", "12"], ["-3", "@SUM(A1)"]])
    z = zipfile.ZipFile(io.BytesIO(b))
    assert z.testzip() is None
    for n in z.namelist():
        minidom.parseString(z.read(n))            # どの部品も、整った XML
    sst = z.read("xl/sharedStrings.xml").decode()
    assert "'=1+1" in sst and "'@SUM(A1)" in sst  # 式として読まれない
    assert "\x00" not in sst and "\x07" not in sst
    assert "&amp;" in sst and "&lt;b&gt;" in sst
    assert "/" not in re.search(r'<sheet name="([^"]*)"', z.read("xl/workbook.xml").decode()).group(1)  # シート名に使えない文字は置き換える


def test_docx_is_a_valid_zip_and_escapes_text():
    b = docgen.make_docx("報告 <1>", [{"heading": "経緯", "paragraphs": ["一行目\n二行目", "a & b"]}])
    z = zipfile.ZipFile(io.BytesIO(b))
    assert z.testzip() is None
    xml = z.read("word/document.xml").decode()
    minidom.parseString(xml)
    assert "&lt;1&gt;" in xml and "a &amp; b" in xml and "<w:br/>" in xml


def test_docgen_caps_sizes():
    b = docgen.make_xlsx("t", [str(i) for i in range(30)], [[str(i)] * 30 for i in range(500)])
    sheet = zipfile.ZipFile(io.BytesIO(b)).read("xl/worksheets/sheet1.xml").decode()
    assert sheet.count("<row ") == docgen.MAX_ROWS + 1
    assert "M1" not in sheet                      # 列の上限（12 列 = L まで）


def test_generated_files_open_with_real_office_libraries_when_available():
    openpyxl = pytest.importorskip("openpyxl")   # 検証にだけ使う（アプリの依存ではない）
    docx = pytest.importorskip("docx")
    ws = openpyxl.load_workbook(io.BytesIO(docgen.make_xlsx("表", ["a", "b"], [["1", "x"]]))).active
    assert [[c.value for c in r] for r in ws.iter_rows()] == [["a", "b"], [1, "x"]]
    assert "経緯" in [p.text for p in docx.Document(io.BytesIO(docgen.make_docx("t", [{"heading": "経緯", "paragraphs": ["x"]}]))).paragraphs]


# ---- AI の答えの検査 --------------------------------------------------------------------------------

TN = talk.transcript_norm(TURNS)


def val(**over):
    d = json.loads(json.dumps(GOOD))
    for k, v in over.items():
        d[k] = v
    return fusion.validate(d, TN, talk.person_names(" ".join(t["text"] for t in TURNS)))


def test_a_good_answer_passes_with_a_circle_box():
    res, why = val()
    assert res and res["targets"][0]["box"] == [0.35, 0.4, 0.3, 0.3] and len(res["targets"]) == 2 and not why


def test_a_quote_that_is_not_in_the_talk_removes_the_circle_but_keeps_the_documents():
    t = dict(GOOD["targets"][0], evidence="会話にない言葉です")
    res, why = val(targets=[t, GOOD["targets"][1]])
    assert res and res["targets"][0]["box"] is None and res["targets"][1]["box"] and any("実在しない" in w for w in why)


@pytest.mark.parametrize("box", [[0, 0, 1, 1], [0.5, 0.5, 0.9, 0.9], [-0.1, 0, 0.2, 0.2], [0.1, 0.1, 0.005, 0.2], "x", [0.1, 0.1, 0.2]])
def test_a_wide_or_out_of_range_box_is_not_drawn(box):
    res, _ = val(targets=[dict(GOOD["targets"][0], box=box)])
    assert res and res["targets"][0]["box"] is None


@pytest.mark.parametrize("field,text", [
    ("email", {"subject": "件名", "body": "電話番号は 090-1234-5678 です"}),
    ("email", {"subject": "件名", "body": "連絡は taro@example.com へ"}),
    ("email", {"subject": "件名", "body": "氏名を確認してください"}),
    ("email", {"subject": "件名", "body": "田中さんへ送ります"}),
    ("email", {"subject": "件名", "body": "全員に公開してください"}),
    ("report", {"title": "t", "sections": [{"heading": "h", "paragraphs": ["口座番号を控える"]}]}),
    ("table", {"title": "t", "columns": ["a"], "rows": [["1234567890"]]}),
])
def test_personal_words_names_long_digits_and_followed_instructions_reject_the_whole_answer(field, text):
    res, why = val(**{field: text})
    assert res is None and why


@pytest.mark.parametrize("word", ["現場担当者様", "関係者様", "各位様", "お客様", "皆様", "ご担当の方様", "縞模様", "同様", "仕様", "多様"])
def test_role_words_with_an_honorific_are_not_treated_as_names(word):
    res, _ = val(email={"subject": "件名", "body": f"{word}へ、資材の件でご連絡します。"})
    assert res is not None


def test_missing_parts_are_rejected():
    assert val(table={"title": "t", "columns": [], "rows": []})[0] is None
    assert val(email={"subject": "", "body": ""})[0] is None
    assert val(report={"title": "t", "sections": []})[0] is None
    assert fusion.validate("x", TN, set())[0] is None


def test_next_work_is_capped_and_only_text():
    many = [{"label": f"作業{i}", "reason": "r"} for i in range(9)]
    res, _ = val(next_work=many)
    assert len(res["next_work"]) == fusion.MAX_NEXT


def test_the_red_circle_is_drawn_on_a_copy_and_only_around_the_target():
    src = screen((100, 60, 200, 120))
    out = fusion.draw_circle(src, [0.3, 0.3, 0.3, 0.3])
    a, b = Image.open(io.BytesIO(src)).convert("RGB"), Image.open(io.BytesIO(out)).convert("RGB")
    assert a.size == b.size
    w, h = a.size
    assert a.getpixel((int(w * 0.45), int(h * 0.45))) is not None
    # 中心と、遠い隅は、変わらない（ただし JPEG の再圧縮の誤差は許す）
    for pt in [(int(w * 0.45), int(h * 0.45)), (5, 5), (w - 5, h - 5)]:
        assert sum(abs(x - y) for x, y in zip(a.getpixel(pt), b.getpixel(pt))) < 40
    reds = sum(1 for x in range(w) for y in range(h) if (lambda p: p[0] > 200 and p[1] < 90 and p[2] < 90)(b.getpixel((x, y))))
    assert reds > 100                              # 赤い線が、描かれている


# ---- 門 ----------------------------------------------------------------------------------------------

def test_it_is_off_by_default(conn, alice, obj_):
    card, image = make_card(conn, alice, obj_)
    assert not fusion.enabled(conn, "org_1")
    with pytest.raises(fusion.FusionRefused, match="オフ"):
        run(conn, alice, card, image)


@pytest.mark.parametrize("off", ["talk_llm", "talk_audio", "vision_llm", "fusion_demo"])
def test_each_of_the_settings_is_required(conn, alice, obj_, off):
    flags(conn, **{off: 0})
    card, image = make_card(conn, alice, obj_)
    assert not fusion.enabled(conn, "org_1")
    with pytest.raises(fusion.FusionRefused, match="オフ"):
        run(conn, alice, card, image)


def test_the_emergency_stop_and_the_consent_are_required(conn, alice, setup, monkeypatch):
    card, image = setup
    with pytest.raises(fusion.FusionRefused, match="確認"):
        run(conn, alice, card, image, consent=False)
    monkeypatch.setenv("MIRUCON_FUSION", "0")
    with pytest.raises(fusion.FusionRefused, match="オフ"):
        run(conn, alice, card, image)


def test_the_image_gates_apply_before_any_audio_leaves(conn, alice, obj_):
    flags(conn, sensitive_industry=1)
    card, image = make_card(conn, alice, obj_)
    f = factory()
    with pytest.raises(vision.VisionRefused, match="機微"):
        run(conn, alice, card, image, f)
    assert f.state["calls"] == []                  # 音声は、外へ出ていない


def test_a_non_call_screen_image_and_a_wide_open_area_are_refused(conn, alice, obj_):
    flags(conn)
    card, image = make_card(conn, alice, obj_, source="camera")
    with pytest.raises(vision.VisionRefused, match="通話の画面"):
        run(conn, alice, card, image)
    card2, image2 = make_card(conn, alice, obj_, img=screen((0, 0, 600, 340)))
    with pytest.raises(vision.VisionRefused, match="広すぎ"):
        run(conn, alice, card2, image2)


def test_only_the_creator_or_the_owner_can_start(conn, alice, bob, setup):
    card, image = setup
    with pytest.raises(fusion.FusionRefused, match="作り手"):
        run(conn, bob, card, image)


def test_audio_limits(conn, alice, setup):
    card, image = setup
    for a, m in [(b"", "ありません"), (b"x" * 1_600_000, "長すぎ")]:
        with pytest.raises(fusion.FusionRefused, match=m):
            run(conn, alice, card, image, audio=a)
    with pytest.raises(fusion.FusionRefused, match="形式"):
        fusion.start(conn, alice, card["card_id"], image["image_id"], WAV, "webm", consent=True, jobs=web.JOBS, client_factory=factory())


def test_runs_per_card_are_capped(conn, alice, setup):
    card, image = setup
    for _ in range(fusion.MAX_RUNS_PER_CARD):
        run(conn, alice, card, image)
    with pytest.raises(fusion.FusionRefused, match="回まで"):
        run(conn, alice, card, image)


# ---- 流れ ---------------------------------------------------------------------------------------------

def test_the_image_is_not_sent_until_the_creator_confirms_the_text(conn, alice, setup):
    card, image = setup
    fid, f = run(conn, alice, card, image)
    st = fusion.get(conn, alice, fid)
    assert st["status"] == "transcribed" and [t["text"] for t in st["transcript"]] == [t["text"] for t in TURNS]
    blocks = [b for c in f.state["calls"] for m in c["messages"] for b in (m["content"] if isinstance(m["content"], list) else [])]
    assert any(b.get("type") == "input_audio" for b in blocks) and not any(b.get("type") == "image" for b in blocks)   # ここまで、画像は渡していない
    assert {c["model"].split("/")[-1] for c in f.state["calls"]} <= {"gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.1-pro-preview"}


def test_audio_is_not_stored(conn, alice, setup):
    card, image = setup
    marker = b"RIFF" + b"\x00" * 4000
    run(conn, alice, card, image, audio=marker)
    for t in ("fusion_run", "fusion_file", "talk_segment", "audit_log"):
        assert marker not in json.dumps([dict(r) for r in db.many(conn, f"SELECT * FROM {t}")], default=lambda o: bytes(o).hex()).encode()


def test_the_whole_flow_produces_files_a_circle_and_a_cheap_single_opus_call(conn, alice, setup):
    card, image = setup
    fid, f = run(conn, alice, card, image)
    st = finish(conn, alice, fid, f)
    assert st["status"] == "done" and st["result"]["table"]["rows"][0][0] == "バケット"
    assert set(st["files"]) == {"table.xlsx", "report.docx", "circled.jpg"}
    fuse_calls = [c for c in f.state["calls"] if "fuse_and_draft" in {t["name"] for t in c["tools"]}]
    assert len(fuse_calls) == 1                    # AI への問い合わせは 1 回
    types = [b.get("type") for m in fuse_calls[0]["messages"] for b in m["content"]]
    assert types.count("image") == 1 and "input_audio" not in types
    text = " ".join(b.get("text", "") for m in fuse_calls[0]["messages"] for b in m["content"])
    assert "来週の火曜日" in text
    mime, data = fusion.download(conn, alice, fid, "table.xlsx")
    assert mime == docgen.XLSX_MIME and zipfile.ZipFile(io.BytesIO(data)).testzip() is None
    assert db.one(conn, "SELECT 1 FROM audit_log WHERE action='fusion.analyzed'")


def test_edits_and_deleted_lines_are_what_the_ai_gets(conn, alice, setup):
    card, image = setup
    fid, f = run(conn, alice, card, image)
    finish(conn, alice, fid, f, edited=["この重機のバケットの周りを、来週の火曜日にもう一度点検してほしいです", ""])   # 2 行目は削除
    fuse = [c for c in f.state["calls"] if "fuse_and_draft" in {t["name"] for t in c["tools"]}][0]
    text = " ".join(b.get("text", "") for m in fuse["messages"] for b in m["content"])
    assert "メールでお送りします" not in text and "バケット" in text
    with pytest.raises(fusion.FusionRefused, match="数"):
        f2 = factory()
        fid2, _ = run(conn, alice, card, image, f2)
        fusion.confirm_and_analyze(conn, alice, fid2, ["a"], jobs=web.JOBS, client_factory=f2)


def test_confirm_only_works_right_after_transcription_and_needs_text(conn, alice, setup):
    card, image = setup
    fid, f = run(conn, alice, card, image)
    finish(conn, alice, fid, f)
    with pytest.raises(fusion.FusionRefused, match="直後"):
        fusion.confirm_and_analyze(conn, alice, fid, None, jobs=web.JOBS, client_factory=f)
    fid2, f2 = run(conn, alice, card, image)
    with pytest.raises(fusion.FusionRefused, match="ありません"):
        fusion.confirm_and_analyze(conn, alice, fid2, ["", ""], jobs=web.JOBS, client_factory=f2)


def test_a_rejected_answer_leaves_no_files_and_a_reason(conn, alice, setup):
    card, image = setup
    bad = dict(GOOD, email={"subject": "s", "body": "電話は 03-1234-5678"})
    fid, f = run(conn, alice, card, image, factory(fuse=bad))
    st = finish(conn, alice, fid, f)
    assert st["status"] == "rejected" and st["result"] is None and st["files"] == [] and st["why"]


def test_an_api_failure_is_a_failed_run_not_an_exception(conn, alice, setup):
    card, image = setup
    def boom(k, i):
        names = {t["name"] for t in k["tools"]}
        if "extract_talk" in names:
            return [("extract_talk", {"turns": TURNS})]
        raise RuntimeError("down")
    fid, f = run(conn, alice, card, image, client_dynamic(boom, echo_model=True))
    st = finish(conn, alice, fid, f)
    assert st["status"] == "failed"


def test_transcription_failure_is_recorded(conn, alice, setup):
    card, image = setup
    f = client_dynamic(lambda k, i: [("extract_talk", {"turns": []})], echo_model=True)
    fid, _ = run(conn, alice, card, image, f)
    assert fusion.get(conn, alice, fid)["status"] == "failed"


def test_settings_can_be_turned_off_between_confirmation_and_analysis(conn, alice, setup):
    card, image = setup
    fid, f = run(conn, alice, card, image)
    flags(conn, vision_llm=0)
    with pytest.raises(vision.VisionRefused):
        fusion.confirm_and_analyze(conn, alice, fid, None, jobs=web.JOBS, client_factory=f)


def test_other_orgs_cannot_read_a_run(conn, alice, setup):
    card, image = setup
    fid, f = run(conn, alice, card, image)
    from app import auth, authz
    mid = auth.add_member(conn, "org_2", "x@example.test", "owner"); conn.commit()
    other = authz.Actor(kind="member", org_id="org_2", member_id=mid, role="owner")
    with pytest.raises(Exception):
        fusion.get(conn, other, fid)


# ---- 共有・送信 -----------------------------------------------------------------------------------------

def done_run(conn, alice, setup):
    card, image = setup
    fid, f = run(conn, alice, card, image)
    finish(conn, alice, fid, f)
    return fid


def test_share_needs_a_folder_the_owner_set_and_copies_only_the_two_documents(conn, alice, owner, setup, tmp_path):
    fid = done_run(conn, alice, setup)
    with pytest.raises(fusion.FusionRefused, match="設定されていません"):
        fusion.share(conn, alice, fid, "table.xlsx")
    with pytest.raises(fusion.FusionRefused, match="オーナー"):
        fusion.set_share_dir(conn, alice, str(tmp_path))
    fusion.set_share_dir(conn, owner, str(tmp_path))
    name = fusion.share(conn, alice, fid, "table.xlsx")
    assert (tmp_path / name).read_bytes() == fusion.download(conn, alice, fid, "table.xlsx")[1]
    with pytest.raises(fusion.FusionRefused, match="すでに"):
        fusion.share(conn, alice, fid, "table.xlsx")
    for bad in ("circled.jpg", "../evil.xlsx", "..\\evil", "report.docx/../../x", "/etc/passwd"):
        with pytest.raises(Exception):
            fusion.share(conn, alice, fid, bad)
    assert sorted(p.name for p in tmp_path.iterdir()) == [name]      # 共有先には、それ以外を書いていない


def test_the_share_dir_must_exist_and_be_absolute(conn, owner, tmp_path):
    for bad in ("relative/dir", str(tmp_path / "nope")):
        with pytest.raises(fusion.FusionRefused, match="絶対パス"):
            fusion.set_share_dir(conn, owner, bad)
    fusion.set_share_dir(conn, owner, str(tmp_path))
    fusion.set_share_dir(conn, owner, "")
    assert fusion.share_dir(conn, "org_1") is None


def test_share_does_not_follow_a_symlink_at_the_destination(conn, alice, owner, setup, tmp_path):
    fid = done_run(conn, alice, setup)
    fusion.set_share_dir(conn, owner, str(tmp_path))
    outside = tmp_path.parent / "outside_target.xlsx"
    link = tmp_path / f"{fid}_table.xlsx"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("シンボリックリンクを作れない環境")
    with pytest.raises(fusion.FusionRefused):
        fusion.share(conn, alice, fid, "table.xlsx")
    assert not outside.exists()


def test_the_gmail_link_only_opens_a_compose_window_without_a_recipient():
    u = fusion.gmail_url({"subject": "再点検 & 確認", "body": "本文\n二行目"}, "共有")
    p = urllib.parse.urlsplit(u)
    q = urllib.parse.parse_qs(p.query)
    assert p.scheme == "https" and p.netloc == "mail.google.com" and q["view"] == ["cm"]
    assert "to" not in q and "cc" not in q and "bcc" not in q
    assert q["su"] == ["再点検 & 確認"] and "共有" in q["body"][0]
    assert len(fusion.gmail_url({"subject": "s", "body": "あ" * 5000}, "共有")) <= fusion.MAX_URL


# ---- 画面・ルート ---------------------------------------------------------------------------------------

def test_settings_are_owner_only_and_audited(conn, owner, alice):
    assert post(conn, "/settings/fusion", {"enabled": "1"}, login(conn, alice)).status in (403, 404)
    assert post(conn, "/settings/fusion", {"enabled": "1"}, login(conn, owner)).status == 303
    assert db.one(conn, "SELECT fusion_demo FROM org_setting WHERE org_id='org_1'")["fusion_demo"] == 1
    assert db.one(conn, "SELECT 1 FROM audit_log WHERE action='org.fusion_demo'")
    assert post(conn, "/settings/fusion-dir", {"dir": "relative"}, login(conn, owner)).status == 400


def test_card_page_offers_the_form_only_when_enabled_and_to_the_creator(conn, alice, bob, obj_):
    card, image = make_card(conn, alice, obj_)
    body = lambda who: get(conn, f"/c/{card['card_id']}", login(conn, who)).body.decode()
    assert "統合分析（会話×共有画面）" not in body(alice)
    flags(conn)
    page = body(alice)
    assert "統合分析（会話×共有画面）" in page and f"/c/{card['card_id']}/fusion/{image['image_id']}" in page and 'enctype="multipart/form-data"' in page
    assert "統合分析（会話×共有画面）" not in body(bob)        # 作り手・オーナー以外には出さない


def test_full_flow_through_the_web_pages(conn, alice, setup, monkeypatch, tmp_path, owner):
    card, image = setup
    f = factory()
    monkeypatch.setattr(web, "CLIENT_FACTORY", f)
    ck = login(conn, alice)
    body, ctype = multipart({"format": "wav", "consent": "1"}, [("audio", WAV)])
    r = web.handle(conn, "POST", f"/c/{card['card_id']}/fusion/{image['image_id']}", {**{"host": "localhost", "cookie": ck}, "content-type": ctype}, body)
    assert r.status == 303
    page = get(conn, f"/c/{card['card_id']}", ck).body.decode()
    assert "音声を文字にしました" in page and 'name="t0"' in page
    fid = db.one(conn, "SELECT fusion_id FROM fusion_run")["fusion_id"]
    assert post(conn, f"/f/{fid}/confirm", {"t0": TURNS[0]["text"], "t1": TURNS[1]["text"]}, ck).status == 303
    fusion.set_share_dir(conn, owner, str(tmp_path))
    page = get(conn, f"/c/{card['card_id']}", ck).body.decode()
    for s in ("聞いたこと", "私はこう理解しました", "作ったもの", "次の選択肢", "油圧ショベルのバケット", "circled.jpg", "https://mail.google.com/mail/?", "共有フォルダへコピー"):
        assert s in page
    r = get(conn, f"/f/{fid}/file/table.xlsx", ck)
    assert r.status == 200 and r.content_type == docgen.XLSX_MIME and any("attachment" in v for k, v in r.headers)
    assert get(conn, f"/f/{fid}/file/circled.jpg", ck).content_type == "image/jpeg"
    assert get(conn, f"/f/{fid}/file/table.xlsx").status == 303               # ログインなしでは、取れない
    assert get(conn, f"/f/{fid}/file/other.txt", ck).status == 404
    assert post(conn, f"/f/{fid}/share/table.xlsx", {}, ck).status == 200
    assert len(list(tmp_path.iterdir())) == 1


def test_the_upload_route_is_allowed_a_large_body_but_others_are_not():
    mp = {"content-type": "multipart/form-data; boundary=x"}
    assert web.body_limit("POST", "/c/c_1/fusion/i_1", mp) == web.MAX_UPLOAD
    assert web.body_limit("POST", "/c/c_1/vision/i_1", mp) == web.MAX_BODY
    assert web.body_limit("POST", "/f/f_1/confirm", mp) == web.MAX_BODY


# ---- 保存期間・持ち運び ------------------------------------------------------------------------------------

def test_old_runs_and_files_are_purged(conn, alice, setup):
    fid = done_run(conn, alice, setup)
    db.run(conn, "UPDATE fusion_run SET created_at=?", (db.now() - 15 * 86400,))
    conn.commit()
    assert fusion.purge_old(conn) == 1 and not db.one(conn, "SELECT 1 FROM fusion_file")


def test_demo_audio_is_found_by_env_dir_not_a_fixed_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRUCON_DEMO_DIR", str(tmp_path))
    assert fusion.demo_audio() is None
    (tmp_path / "demo_voice.wav").write_bytes(WAV)
    assert fusion.demo_audio() == (WAV, "wav")
    (tmp_path / "demo_voice.wav").write_bytes(b"x" * 1_600_000)
    assert fusion.demo_audio() is None                                        # 上限を超える音声は使わない


def test_new_modules_have_no_hardcoded_absolute_paths():
    import pathlib
    for n in ("fusion.py", "docgen.py", "theme.py"):
        src = (pathlib.Path(fusion.__file__).parent / n).read_text(encoding="utf-8")
        assert not re.search(r"[A-Za-z]:\\\\|C:/Users|/home/|/Users/", src), n


# ---- 見た目（追加のみ）--------------------------------------------------------------------------------------

def test_theme_is_added_after_the_existing_css_and_respects_reduced_motion_and_dark_mode():
    r = web.page("t", "<p>x</p>")
    html = r.body.decode()
    assert html.index(web.CSS) < html.index("--blue:#2f80ff")                 # 既存の CSS は残り、後ろに重ねる
    assert 'class="fx" aria-hidden="true"' in html
    assert "prefers-reduced-motion:reduce" in theme.THEME and "prefers-color-scheme:dark" in theme.THEME
    assert "pointer-events:none" in theme.THEME
    assert "url(" not in theme.THEME and "@import" not in theme.THEME         # 外部のファイルを使わない（CSP）
    m = re.search(r"\.fx svg\{[^}]*opacity:([.\d]+)", theme.THEME)
    assert float(m.group(1)) <= 0.2                                            # 目立たせない
    assert "script-src 'self'" in dict(r.all_headers())["Content-Security-Policy"]


def test_the_fusion_and_docgen_modules_work_from_a_copied_app_folder_with_no_demo_data(tmp_path):
    """app/ だけを別の場所へコピー（demo データもなし）して、統合分析・資料生成・見た目が動くこと。"""
    import shutil
    import subprocess
    import sys
    dest = tmp_path / "elsewhere" / "app"
    shutil.copytree(pathlib_app(), dest, ignore=shutil.ignore_patterns("tests", "data", "__pycache__", "*.db"))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    code = ("import app.fusion as f, app.docgen as d, app.theme as t, app.web as w\n"
            "assert f.demo_audio() is None\n"
            "assert len(d.make_xlsx('t', ['a'], [['1']])) > 100\n"
            "assert f.gmail_url({'subject': 's', 'body': 'b'}).startswith('https://mail.google.com/')\n"
            "assert 'fx' in w.page('t', '').body.decode()\n"
            "print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       env={"PYTHONPATH": str(dest.parent), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "PATH": os.environ.get("PATH", "")})
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-500:]


def pathlib_app():
    import pathlib
    return pathlib.Path(fusion.__file__).parent


# ---- 自分の Gmail アドレス（任意）-------------------------------------------------------------------

@pytest.mark.parametrize("addr", ["me@gmail.com", "a.b+c@googlemail.com", "ME@GMAIL.COM"])
def test_a_gmail_address_becomes_the_recipient_and_the_account(addr):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(fusion.gmail_url({"subject": "s", "body": "b"}, "共有", addr)).query)
    assert q["to"] == [addr] and q["authuser"] == [addr] and q["view"] == ["cm"]


@pytest.mark.parametrize("addr", ["", None, "me@example.com", "me@gmail.com.evil.test", "a b@gmail.com", "me@gmail.com,x@y.z", "x@gmail.co", "me@gmail.com&bcc=x@y.z"])
def test_anything_but_a_gmail_address_is_not_used(addr):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(fusion.gmail_url({"subject": "s", "body": "b"}, "", addr)).query)
    assert "to" not in q and "authuser" not in q and "bcc" not in q


def test_the_url_stays_within_the_limit_with_an_address():
    assert len(fusion.gmail_url({"subject": "s", "body": "あ" * 5000}, "共有", "someone.long.address@gmail.com")) <= fusion.MAX_URL


def test_a_member_sets_and_clears_their_own_address_without_logging_it(conn, alice, bob):
    fusion.set_gmail(conn, alice, "alice.me@gmail.com")
    assert fusion.get_gmail(conn, alice) == "alice.me@gmail.com" and fusion.get_gmail(conn, bob) == ""   # 他のメンバーには効かない
    assert "alice.me" not in json.dumps([dict(r) for r in db.many(conn, "SELECT * FROM audit_log")])
    for bad in ("x@example.com", "not an address"):
        with pytest.raises(fusion.FusionRefused, match="Gmail"):
            fusion.set_gmail(conn, alice, bad)
    assert fusion.get_gmail(conn, alice) == "alice.me@gmail.com"                                        # 不正な入力では変わらない
    fusion.set_gmail(conn, alice, "")
    assert fusion.get_gmail(conn, alice) == ""


def test_the_setting_form_shows_only_when_enabled_and_the_link_uses_the_members_address(conn, alice, obj_):
    ck = login(conn, alice)
    assert "/settings/gmail" not in get(conn, "/", ck).body.decode()
    card, image = make_card(conn, alice, obj_)
    flags(conn)
    assert "/settings/gmail" in get(conn, "/", ck).body.decode()
    assert post(conn, "/settings/gmail", {"address": "me.demo@gmail.com"}, ck).status == 303
    assert post(conn, "/settings/gmail", {"address": "x@example.com"}, ck).status == 400
    f = factory()
    fid = fusion.start(conn, alice, card["card_id"], image["image_id"], WAV, "wav", consent=True, jobs=web.JOBS, client_factory=f)
    fusion.confirm_and_analyze(conn, alice, fid, None, jobs=web.JOBS, client_factory=f)
    page = get(conn, f"/c/{card['card_id']}", ck).body.decode()
    assert "to=me.demo%40gmail.com" in page and "authuser=me.demo%40gmail.com" in page
    post(conn, "/settings/gmail", {"address": ""}, ck)
    assert "to=me.demo" not in get(conn, f"/c/{card['card_id']}", ck).body.decode()
