# Connect-Force

Connect-Forceは、現場の記録、会話、画面上の情報を根拠とともに整理し、人の承認を通して次の業務へつなぐAIエージェントです。設計の中心は「AIが選び、コードが守り、人が確定する」です。

![Connect-Force](./hackathon-kit/assets/connect-force-hero.png)

## 体験できること

- **物エージェント**: QR・NFCで物や場所の担当・記録・経緯を開き、マスキング済みの情報からカード下書きを作成
- **統合分析**: 会話と共有画面をつなぎ、対象物の確認、表・説明・下書きの作成を支援
- **Mission Room**: 設備IDから履歴、注意点、条件付き対応案、承認待ちを一画面で確認
- **音声分析**: 明示送信した短いマイク音声をORCA ROUTERで分析し、要約は利用者の確認後に利用

## 審査員向けの起動方法

### Python版 物エージェント

Python 3.11以上を用意します。

```bash
pip install -r requirements.txt
python -m pytest app/tests -q
MIRUCON_ENV=dev PORT=8000 python -m app.web owner@example.test
```

ブラウザで `http://127.0.0.1:8000/login` を開きます。開発用のワンタイムコードは起動したターミナルに表示されます。

### 統合分析で、自分の Gmail アドレスを使う（任意）

トップ画面の「自分の Gmail アドレス」に、自分の gmail.com のアドレスを設定すると、統合分析の「送信の準備」で開く Gmail の作成画面の宛先（To）と開くアカウントに入ります（統合分析がオンのとき表示）。未設定なら宛先なしで開きます。アドレスは、本人だけが設定でき、gmail.com / googlemail.com 以外は受け付けません。送信は、Gmail で人が行います。

### Node.js版 Mission Room

Node.js 24以上とpnpmを用意します。

```powershell
pnpm install
.\scripts\run.ps1 pc
```

PowerShellに表示される一回限りのログインURLを開き、「合成デモを試す」を選びます。根拠表示、対応案の比較、内容確認、承認保存までを体験できます。

## データ配置

公開リポジトリでは、利用者ごとのデータ用フォルダを `your_folder` と表記します。実際のVaultを使う場合は、Markdownを `your_folder/vault/` に置くか、起動前に絶対パスを環境変数で設定します。

```powershell
$env:CONNECT_FORCE_VAULT_ROOT = "D:\\work\\your_folder\\vault"
```

実データ、生成物、環境変数の値はGitへ追加しません。

## 実装・検証

- セキュリティ: 入力検査、端末側マスキング、承認、CSRF、限定した音声送信経路をコードで制御
- コスト: Mission Roomの通常分析はモデル呼出し0回。音声分析は回数と予約予算を制限
- 信頼性: 43件の自動・HTTP統合テスト、型検査、Eve構成検査、本番ビルド、秘密情報スキャンを実施
- ORCA ROUTER E2E: 27.684秒の音声を3回連続で処理し、すべてHTTP 200で完了。合計見積は$0.0011394、デモ上限は$2

APIキーは環境変数で設定し、ソース、Git、チャット、画面へ貼り付けません。

## ハッカソン資料

- [既存の提出資料](./hackathon_materials/)
- [Mission Room発表資料](./hackathon-kit/Connect-Force_ハッカソン発表資料_20260921_E2E_最終版_v3.pptx)
- [来場者向けパンフレット](./hackathon-kit/Connect-Force_来場者パンフレット.pdf)

## ライセンス

ハッカソン提出用のソースです。第三者サービスの利用条件と組織の情報管理ルールに従って利用してください。
