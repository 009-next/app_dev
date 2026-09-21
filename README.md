# Connect-Force（物エージェント）

物や場所に貼った QR・NFC にかざすと、その物の**担当・記録・これまでの経緯**が開き、作業の成果は「動くカード」にしてリンクで渡せます。写真の顔・ナンバー・書類は、**送る前に端末で隠します**。

その中で状況を見て処理を選ぶのが**物エージェント**です。設計は「**AI が選び、コードが守り、人が確定する**」。

- 会話（音声）から次の業務を提案し、作り手が確認・同意したものだけ下書きにします。
- 通話の画面は「開けた所だけ」を AI が見ます（全面を隠し、なぞって囲んだ所だけを開ける）。
- **統合分析**：会話の音声と共有画面を突き合わせ、対象物に**赤丸**をつけ、**Excel の表・Word の説明・メールの下書き**まで作ります。共有・送信は、人が押したときだけです。

デモ動画・パンフレット・記事は [`hackathon_materials/`](hackathon_materials/) にあります（`demo_video/unified_analysis_demo.mp4`）。

## すぐ動かす（審査員の方へ）

必要なもの: Python 3.11 以上。

```bash
pip install -r requirements.txt
python -m pytest app/tests -q                      # 自動テスト（AI の呼び出しは偽の応答。キー不要）
MIRUCON_ENV=dev PORT=8000 python -m app.web owner@example.test
```

1. ブラウザで `http://127.0.0.1:8000/login` を開き、`owner@example.test` を入力します。
2. **ワンタイムコードは、起動したターミナルのログに表示されます**（開発モード）。
3. ログインすると、物の登録・カードの作成ができます。AI の機能は、キーがなくても既定の動作で動きます。

Windows の PowerShell では、`$env:MIRUCON_ENV="dev"; python -m app.web owner@example.test` のように環境変数を設定してください。

### AI を実際に使う場合（任意）

キーは**環境変数**で渡します（ファイルに書かない・コミットしない）。

```bash
export ORCA_API_KEY=your_api_key_here      # Orca Router 経由（主）
# または
export ANTHROPIC_API_KEY=your_api_key_here # Claude 直（予備）
```

### 統合分析を試す

トップ画面（オーナー）で、次の 4 つを設定します（すべて既定オフ）。

1. 「画像を AI に見せる機能」をオン
2. 「会話から次の業務を考える機能」をオン、その中の「音声分析」もオン
3. 「統合分析」をオン、共有先のフォルダ（サーバー上の絶対パス）を指定
4. カード作成で、通話の画面（画面共有、または動画ファイルを「通話の録画として取り込む」）を取り込み、開ける所をなぞって囲む

カードのページに「統合分析」が出ます。デモ用の音声（wav・16kHz・モノラル・約 45 秒まで）を `app/data/demo/demo_voice.wav` に置くか、環境変数 `MIRUCON_DEMO_DIR` でフォルダを指定すると、「デモ用ボイスで始める」ボタンが出ます。

## 構成

```
app/                 アプリ本体（Python 標準ライブラリ＋anthropic・openai・Pillow）
  web.py             画面・ルート            agent.py     AI の判断（4 段階）
  vision.py          通話画面の分析          talk*.py     会話から次の業務・音声分析
  fusion.py          統合分析                docgen.py    xlsx・docx の生成（標準ライブラリのみ）
  theme.py           見た目                  static/      端末側（mask.js: 隠す・開ける）
  tests/             自動テスト
docs/ARCHITECTURE.md 設計の要点
verification/        検証・録画用のスクリプト
hackathon_materials/ 資料（記事・スライド・パンフレット・デモ動画・図）
```

## 守り方（要点）

- AI に渡さない操作: リンク発行・共有範囲の拡大・ぼかしの削減・削除・送信（ツールとして持たない）。
- 会話・画像を AI に渡す機能は既定オフ。組織のオーナーの設定と、作り手のそのつどの確認が揃ったときだけ動く。
- 音声は保存しない。文字を確認するまで、画像は AI に渡さない。
- AI の出力は、コードが検査する（引用の実在・個人情報・人名・指示への追従）。
- 自動テスト 839 件。門を 1 つずつ外すと、テストが失敗することを確認している。

詳しくは [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) を参照してください。

## 素材について

`verification/assets/` には、デモ用の画像・音声を置きます（リポジトリには含めていません）。手元の画像・音声で、同じ流れを試せます。
