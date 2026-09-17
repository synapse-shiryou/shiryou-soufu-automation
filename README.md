# 資料送付報告 自動化

営業架電の音声ファイルをアップロードすると、Gemini APIで文字起こし・要約を行い、
アップロード画面で指定したGoogleスプレッドシート(管理表)に自動で書き込むFastAPIアプリです。

## 構成

```
.
├── shiryou_soufu_multi_client.py   # FastAPIアプリ本体 + バックグラウンドワーカー
├── templates/
│   └── upload_form.html            # 音声アップロードフォーム(Jinja2)
├── admin_scripts/                  # 運用・移行用の補助スクリプト(README参照)
├── requirements.txt
└── Procfile                        # Railway/Heroku用起動コマンド
```

- アップロードURLは常に **`/shiryou-soufu/upload`** の1つだけ。クライアントの事前登録は不要
- アップロードは受付のみ同期処理し、即座にレスポンスを返す
- Gemini処理・シート書き込みはAPScheduler製バックグラウンドワーカーが15秒間隔で処理
- どのスプレッドシートに書き込むかは、アップロード画面で毎回貼り付けるURLだけで決まる

## アップロードフォームの入力項目

| 項目 | 必須 | 説明 |
|---|---|---|
| 電話番号 | ○ | 管理表内の行を特定するためのキー |
| お名前 | 任意 | 顧客側の名前(架電担当者ではない) |
| スプレッドシートURL | ○ | 書き込み先の管理表URL。`gid` 付きならそのタブを直接使用、無ければタブ名に「資料送付」を含むシートを自動検索する |
| 確度 | ○ | 高 / 中 / 低 から選択。書き込み先のM列に反映される |
| 実施者 | ○ | 架電担当者本人を `staff_members` テーブルの一覧から選択。Slack通知の実施者表示に使う |
| 架電音声ファイル | ○ | Geminiが文字起こし・要約する音声 |

列(電話番号列・詳細列)はヘッダー行の文字列から自動特定されるため、シートごとの個別設定は不要です。

## 必要な環境変数

| 変数名 | 必須 | 説明 |
|---|---|---|
| `GEMINI_API_KEY` | ○ | Google AI Studioで発行するGemini APIキー |
| `DATABASE_URL` | ○ | PostgreSQL接続文字列。例: `postgresql://user:pass@host:5432/dbname`(RailwayでPostgreSQLプラグインを追加すると自動発行される) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ○(Railway等) | Google Sheets APIサービスアカウントの秘密鍵JSONの中身をそのまま文字列で設定(ファイルアップロードが困難な環境向け) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | 任意 | ローカル実行時、JSONファイルのパスで認証する場合に使用(デフォルト: `service_account.json`)。`GOOGLE_SERVICE_ACCOUNT_JSON` が設定されている場合はそちらが優先される |
| `UPLOAD_DIR` | 任意 | 音声一時保存先ディレクトリ(デフォルト: `/tmp/shiryou_soufu_uploads`) |
| `SLACK_WORKFLOW_WEBHOOK_URL` | 任意 | 書き込み成功時に起動するSlackワークフロー(資料送付報告_v2)のWebhook URL。未設定なら通知はログ出力のみ |
| `SLACK_BOT_TOKEN` | 任意 | 実施者一覧をSlackチャンネルのメンバーと毎日自動同期するためのBot Token(`channels:read`, `users:read`)。未設定なら同期をスキップし、手動登録のみで運用可能 |
| `STAFF_SYNC_CHANNEL_ID` | 任意 | 実施者一覧の同期元とするSlackチャンネルID(デフォルト: `#13_全体連絡チャンネル` = `C0B87GX6RCG`) |

サービスアカウントには、書き込み対象の各Googleスプレッドシートを「編集者」として共有しておく必要があります
(100件超のシートに一括で共有するには `admin_scripts/bulk_share_service_account.py` を参照)。

## ローカル起動方法

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export GEMINI_API_KEY="xxxx"
export DATABASE_URL="postgresql://user:pass@localhost:5432/shiryou_soufu"
export GOOGLE_SERVICE_ACCOUNT_FILE="service_account.json"   # またはGOOGLE_SERVICE_ACCOUNT_JSON

uvicorn shiryou_soufu_multi_client:app --reload
```

起動後、`http://127.0.0.1:8000/shiryou-soufu/upload` にアクセスするとアップロードフォームが表示されます。

初回起動時に `audio_jobs` / `staff_members` テーブルが自動作成されます
(旧バージョンの `client_sheets` テーブル・`audio_jobs.client_code` 列は自動的に削除されます)。

## Railwayへのデプロイ手順

1. **GitHub連携**
   - Railwayダッシュボードで `New Project` → `Deploy from GitHub repo` を選択
   - このリポジトリ(`shiryou-soufu-automation`)を選択してデプロイ
2. **PostgreSQL追加**
   - プロジェクト内で `New` → `Database` → `Add PostgreSQL` を選択
   - 追加すると `DATABASE_URL` が自動生成され、同プロジェクト内の他サービスから参照可能になる
3. **環境変数設定**
   - デプロイしたWebサービスの `Variables` タブで上記の環境変数を設定
4. **Generate Domain**
   - Webサービスの `Settings` → `Networking` → `Generate Domain` で公開URLを発行
   - 発行されたURL + `/shiryou-soufu/upload` を架電担当者に共有

## 実施者(架電担当者)の管理

`staff_members` テーブルに `name`(表示名)と `slack_user_id` を登録しておくと、
アップロードフォームの「実施者」プルダウンに表示され、Slack通知に実施者名が載ります。

`SLACK_BOT_TOKEN` を設定すると、`STAFF_SYNC_CHANNEL_ID` で指定したSlackチャンネルの
メンバー一覧を1日1回(起動時にも1回)自動取得し、`staff_members` テーブルを最新化します
(Bot/削除済みユーザーは除外)。手動登録と併用可能で、同名が既にあれば `slack_user_id` を上書きします。

手動で登録・更新したい場合は以下のSQLも利用できます:

```sql
INSERT INTO staff_members (name, slack_user_id)
VALUES ('山田太郎', 'U0XXXXXXXXX')
ON CONFLICT (name) DO UPDATE SET slack_user_id = EXCLUDED.slack_user_id;
```

`slack_user_id` はSlackのプロフィールやSlack APIの `users.list` / `conversations.members` で確認できます
(実名・メールアドレス等の個人情報を含むCSVやSQLはリポジトリにコミットしないでください)。

## Slack通知の接続

- `notify_slack_success` / `notify_slack_no_match` / `notify_slack_failure` はログ出力のみのスタブです。既存のSlack連携がある場合はこの3関数の中身を差し替えてください。
- `trigger_shiryou_soufu_workflow` は、書き込み成功時にSlackワークフロー「資料送付報告_v2」をWebhook経由で起動します(`official_deal_name`=スプレッドシートのタイトル、`spreadsheet_url`、`uploader_mention`=実施者の表示名を渡す)。Slack側のWebhookトリガーの変数名・データタイプ(Slack ユーザー ID / 表示名)と一致させる必要があります。

## 管理用スクリプト (admin_scripts/)

- `bulk_share_service_account.py`: 指定したGoogleアカウントがアクセスできる全スプレッドシートのうち、タブ名に「資料送付」を含むものを検出し、サービスアカウントを編集者として一括共有する。実行方法はファイル内のdocstringを参照。
- 実名・メールアドレス・Slack IDなど個人情報を含む生成物(`staff_members_raw.txt`, `insert_staff_members.sql` 等)は `.gitignore` により非公開のまま管理すること。
