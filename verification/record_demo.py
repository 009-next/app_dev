#!/usr/bin/env python3
"""統合分析のデモの通しを、実ブラウザ（インストール済みの Chrome）で実行して、動画と画面の画像に記録する。検証・録画用の道具（アプリの依存ではない）。

    python record_demo.py --yes [--out 出力フォルダ]

流れ（senario.txt に対応）:
  1. site.jpg を 1 コマにした動画を、「通話の録画として取り込む」で取り込む（画面共有が使えないときの代わりの入力）。
  2. なぞって囲む形で、カラーコーンと単管の所だけを開ける（面積を端末が測って表示）。
  3. カードを作る → 統合分析: デモ用ボイスを文字にする → 確認 → 会話と画面の意味統合 → 赤丸・表・文書・メール下書き。
  4. 次の選択肢: 共有（指定フォルダへコピー）・送信（Gmail の作成画面のリンク。開かない）。
- 実 API: 音声の文字化（Gemini）と統合分析（Claude の最上位）だけ。カード作成の AI は偽の応答（費用なし）。キーは ORCA_API_KEY（表示しない）。
- 動画には、各手順の字幕（画面下）を入れる。字幕は録画用で、アプリには入っていない。
"""
from __future__ import annotations

import argparse, json, os, pathlib, shutil, subprocess, sys, tempfile, threading, time
from http import server
from types import SimpleNamespace

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
os.environ.setdefault("MIRUCON_LLM_PROFILE", "orca")
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MIRUCON_DEMO_DIR", str(HERE / "assets"))   # デモ用ボイスのフォルダ
from app import auth, authz, db, jobs, llm, objects, web  # noqa: E402
from app.tests.fakes import client_dynamic  # noqa: E402

PORT = 8031
BASE = f"http://127.0.0.1:{PORT}"
C = {"reason": "r", "evidence": ""}
REAL_TOOLS = {"extract_talk", "fuse_and_draft"}
# 開ける形（1536×1024 の画素）: カラーコーンと単管の束
POLY = [(1190, 700), (1330, 672), (1440, 715), (1536, 760), (1536, 1020), (960, 1020), (940, 900), (1080, 800), (1190, 790)]


def fake_script(kw, i):
    n = {t["name"] for t in kw["tools"]}
    if "propose_extra_mask" in n: return [("no_action", C)]
    if "select_card_type" in n: return [("select_card_type", {**C, "type_id": "maintenance"})]
    if "update_summary" in n: return [("hold_summary", C)]
    return [("write_card_text", {"title": "資材の確認", "changes": ["資材の不足を確認"], "description": "現場の資材の確認"})]


def make_router():
    fake = client_dynamic(fake_script, echo_model=True)
    reals: dict = {}

    def factory(provider):
        def create(**kw):
            if {t["name"] for t in kw.get("tools", [])} & REAL_TOOLS:
                if provider not in reals:
                    reals[provider] = llm.default_factory(provider, 100.0)
                return reals[provider].messages.with_raw_response.create(**kw)
            return fake(provider).messages.with_raw_response.create(**kw)
        return SimpleNamespace(messages=SimpleNamespace(with_raw_response=SimpleNamespace(create=create)))
    return factory


def caption(page, text):
    page.evaluate("""t => { let d = document.getElementById('cap'); if (!d) { d = document.createElement('div'); d.id = 'cap';
        d.style.cssText = 'position:fixed;pointer-events:none;left:0;right:0;bottom:0;background:rgba(10,30,70,.9);color:#fff;font:600 20px system-ui;padding:12px 18px;z-index:99999;text-align:center';
        document.body.appendChild(d); } d.textContent = t; }""", text)


def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(); ap.add_argument("--yes", action="store_true"); ap.add_argument("--out", default=str(ROOT / "hackathon_materials" / "demo_video")); a = ap.parse_args()
    if not a.yes:
        print("dry-run（--yes で実行。実 API を使います）"); return
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix="demo_")); share = work / "共有フォルダ"; share.mkdir()
    dbp = work / "demo.db"
    conn = db.connect(dbp); db.init(conn)
    db.run(conn, "INSERT INTO org VALUES(?,?,?,?)", ("org_demo", "デモ工務店", "info@example.test", db.now()))
    mid = auth.add_member(conn, "org_demo", "owner@example.test", "owner")
    db.run(conn, "INSERT INTO org_setting(org_id, external_llm, talk_llm, talk_audio, vision_llm, fusion_demo, fusion_share_dir) VALUES('org_demo',1,1,1,1,1,?)", (str(share),))
    conn.commit()
    actor = authz.Actor(kind="member", org_id="org_demo", member_id=mid, role="owner")
    obj = objects.register_object(conn, actor, "建設現場A")[0]
    web.CLIENT_FACTORY = make_router()
    web.JOBS = jobs.Threaded(lambda: db.connect(dbp))
    frame_mp4 = work / "frame.mp4"   # 画像を 1 コマにした動画（Chrome で再生できる H.264）
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(HERE / "assets" / "site.jpg"), "-t", "3", "-r", "30", "-vf", "scale=1536:1024,format=yuv420p", "-c:v", "libx264", "-movflags", "+faststart", str(frame_mp4)], check=True)
    Base = web.make_handler(conn)

    class H(Base):
        def _run(self):
            if self.path.startswith("/demo-assets/frame.mp4"):   # 録画用の道具だけ（アプリの経路ではない）
                b = frame_mp4.read_bytes()
                self.send_response(200); self.send_header("Content-Type", "video/mp4"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
                return
            return Base._run(self)
        do_GET = do_POST = _run
        def log_message(self, *a, **k): pass

    srv = server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    from playwright.sync_api import sync_playwright
    log: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        vid_dir = work / "video"
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, record_video_dir=str(vid_dir), record_video_size={"width": 1280, "height": 800}, locale="ja-JP")
        conn2 = db.connect(dbp)
        code = auth.request_login_code(conn2, "owner@example.test"); conn2.commit()
        r = ctx.request.post(f"{BASE}/login/verify", form={"email": "owner@example.test", "code": code}, max_redirects=0)
        assert r.status == 303, r.status
        page = ctx.new_page(); page.set_default_timeout(150000)

        def shot(name):
            page.screenshot(path=str(out / f"{name}.png")); log.append(name)

        # 1) 取り込み
        page.goto(f"{BASE}/o/{obj['obj_id']}/new"); caption(page, "1. 共有画面（site.jpg を 1 コマにした動画）を、通話の録画として取り込む"); page.wait_for_timeout(1500)
        page.select_option("#mask-mode", "extended"); page.select_option("#mask-tool", "path"); page.check(".mask-rec-as-call")
        page.evaluate("""async () => { const r = await fetch('/demo-assets/frame.mp4'); const b = await r.blob(); const f = new File([b], 'call.mp4', {type: 'video/mp4'});
            const dt = new DataTransfer(); dt.items.add(f); const i = document.querySelector('.mask-widget[data-role=before] .mask-file'); i.files = dt.files;
            i.dispatchEvent(new Event('change', {bubbles: true})); }""")
        page.wait_for_selector(".mask-widget[data-role=before] .mask-pick:not([hidden])"); page.wait_for_timeout(800)
        # 動画を少し進めて、コマを表示させる（読み込み直後は、コマがまだ描かれていないことがある。実際の操作でも、動画を少し進めてから押す）
        page.evaluate("""async () => { const v = document.querySelector('.mask-widget[data-role=before] .mask-video'); v.currentTime = 0.6;
            await new Promise((r) => { v.onseeked = r; setTimeout(r, 4000); }); }""")
        page.wait_for_timeout(800)
        page.click(".mask-widget[data-role=before] .mask-pick"); page.wait_for_selector(".mask-widget[data-role=before] canvas:not([hidden])"); page.wait_for_timeout(800)
        shot("01_全面ぼかし")
        # 2) なぞって囲む（対象物だけ開ける）
        caption(page, "2. 全部隠れた画面で、対象の物だけを、なぞって囲んで開ける（開けた面積を端末が測る）")
        cv = page.locator(".mask-widget[data-role=before] canvas"); cv.scroll_into_view_if_needed(); bb = cv.bounding_box(); k = bb["width"] / 1536.0
        px = lambda p: (bb["x"] + p[0] * k, bb["y"] + p[1] * k)
        page.mouse.move(*px(POLY[0])); page.mouse.down()
        for p in POLY[1:] + [POLY[0]]:
            page.mouse.move(*px(p), steps=8)
        page.mouse.up(); page.wait_for_timeout(1000)
        area = page.locator(".mask-widget[data-role=before] .mask-area").inner_text(); log.append("area: " + area); print("面積表示:", area)
        shot("02_なぞって開けた")
        page.check("#mask-confirmed"); page.fill("textarea[name=before_desc]", "現場の資材を確認"); page.wait_for_timeout(600)
        page.click("button:has-text('作成')"); page.wait_for_url("**/c/**"); page.wait_for_timeout(1000)
        # 3) 統合分析
        caption(page, "3. 電話の音声（デモ用ボイス）を文字にして、確認してもらう"); page.evaluate("window.scrollTo(0, document.body.scrollHeight)"); page.wait_for_timeout(1200)
        shot("03_統合分析の開始")
        page.check("input[name=consent]"); page.click("button:has-text('デモ用ボイスで始める')")
        page.wait_for_selector("text=音声を文字にしました", timeout=120000); caption(page, "音声を文字にしました。間違いがあれば、ここで直します（確認するまで、画像は AI に渡しません）")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)"); page.wait_for_timeout(2500); shot("04_文字の確認")
        caption(page, "4. 確認した文字と、フィルター後の画面を突き合わせて、資料・メールの下書きを作る（30〜45 秒）")
        page.click("button:has-text('この文字で、統合分析する')")
        page.wait_for_selector("text=次の選択肢", timeout=150000); page.wait_for_timeout(1500)
        steps = page.locator(".flow .step")
        for i in range(steps.count()):
            steps.nth(i).scroll_into_view_if_needed(); caption(page, ["聞いたこと（確認済み）", "見えたもの・赤丸（AI の理解）", "作ったもの（表・文書・メール下書き）", "次の選択肢（共有／送信の準備）"][min(i, 3)])
            page.wait_for_timeout(2600); steps.nth(i).screenshot(path=str(out / f"05_結果_{i + 1}.png")); log.append(f"05_結果_{i + 1}")
        # 4) 共有・送信
        caption(page, "5. 「共有」: 表を、オーナーが決めたフォルダへコピー／「送信の準備」: Gmail の作成画面のリンク（開くだけ・送信は人）")
        link = page.locator("a:has-text('送信の準備')"); href = link.get_attribute("href"); log.append("gmail: " + href[:60] + "…")
        assert href.startswith("https://mail.google.com/mail/?view=cm") and "to=" not in href
        link.scroll_into_view_if_needed(); page.wait_for_timeout(1800)
        page.click("button:has-text('表を共有フォルダへコピー')"); page.wait_for_selector("text=共有しました"); page.wait_for_timeout(2000); shot("06_共有した")
        copied = sorted(p.name for p in share.iterdir()); print("共有フォルダ:", copied); log.append(f"共有フォルダ: {copied}")
        # 結果の取得
        run = db.one(conn2, "SELECT status, model, cost_usd, why FROM fusion_run ORDER BY created_at DESC LIMIT 1")
        print("統合分析:", dict(run))
        caption(page, "完了。AI は下書きまで。共有・送信は、人が押したときだけ"); page.wait_for_timeout(2500)
        ctx.close(); browser.close()
    webm = next(vid_dir.glob("*.webm"))
    mp4 = out / "unified_analysis_demo.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(webm), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", "-movflags", "+faststart", str(mp4)], check=True)
    (out / "record_log.json").write_text(json.dumps({"steps": log, "fusion": dict(run), "shared": copied}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("動画:", mp4, mp4.stat().st_size, "バイト")


if __name__ == "__main__":
    main()
