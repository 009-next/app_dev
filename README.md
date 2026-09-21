# Connect-Force

Connect-Forceは、現場の記録を根拠とともに集め、対応案を下書きとして提示し、人の承認後に業務へ反映するAIエージェントです。ハッカソンのデモでは、設備IDから「過去・現在・未来・行動」を一つのMission Roomにまとめます。

![Connect-Force](./hackathon-kit/assets/connect-force-hero.png)

## できること

- **サイコメトリー**: 設備IDに紐づく履歴を、ファイル名・行番号とともに表示
- **フォース・センス**: 再発、期限、矛盾を検知し、確認が必要な条件を明示
- **フォース・ビジョン**: 根拠をもとに、点検・修理評価・条件付き監視の対応案を比較
- **組分け帽子**: 依頼の目的、危険度、根拠、予算に応じて処理を選択
- **フォース・プッシュ**: 下書きを作成し、利用者が承認した場合だけ保存
- **ポートキー**: 同じPC・同じログインで使える一回限りの作業室移動

## すぐに試す

Node.js 24以上とpnpmを用意します。

```powershell
pnpm install
.\scripts\run.ps1 pc
```

PowerShellに表示される一回限りのログインURLを開き、「合成デモを試す」を選びます。認証、根拠表示、対応案の比較、内容確認、承認保存までを体験できます。

## データ配置

リポジトリ内のデータ用フォルダ名は `your_folder` です。実際のVaultを使う場合は、Markdownを `your_folder/vault/` に置くか、起動前に絶対パスを環境変数で設定します。

```powershell
$env:CONNECT_FORCE_VAULT_ROOT = "D:\\work\\your_folder\\vault"
```

カード下書きと承認済みMission下書きは、ローカルの `data/` 配下に保存されます。実データ、生成物、環境変数の値はGitへ追加しません。

## 音声分析

PC版の通常分析はルール処理で完結し、外部LLMを呼びません。任意のORCA ROUTER音声分析は、利用者の明示確認、最大30秒、マイク入力、固定送信先、回数・予約予算上限を満たす場合だけ有効になります。

2026年9月21日の実環境E2Eでは、27.684秒の音声を3回連続で処理し、すべてHTTP 200で完了しました。各回は766入力token・60出力tokenで、公開単価に基づく合計見積は$0.0011394です。デモの上限は$2です。

APIキーは環境変数で設定します。値をソース、Git、チャット、画面へ貼り付けないでください。

```powershell
$env:ORCAROUTER_API_KEY = "ORCA ROUTERで新規発行したキー"
$env:CONNECT_FORCE_ENABLE_AUDIO_SEND = "yes"
```

## 検証

```powershell
.\scripts\verify.ps1 -Full
```

2026年9月21日時点で、43件の自動・HTTP統合テスト、型検査、Eve構成検査、本番ビルド、秘密情報スキャンが成功しています。

## ハッカソン資料

- [発表資料](./hackathon-kit/Connect-Force_ハッカソン発表資料_20260921_E2E_最終版_v3.pptx)
- [Qiita記事下書き](./hackathon-kit/qiita-connect-force.md)
- [Zenn記事下書き](./hackathon-kit/zenn-connect-force.md)
- [来場者向けパンフレット](./hackathon-kit/Connect-Force_来場者パンフレット.pdf)

## ライセンス

ハッカソン提出用のソースです。第三者サービスの利用条件と組織の情報管理ルールに従って利用してください。
