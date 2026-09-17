# 資料送付報告 自動化(マルチクライアント)

営業架電の音声ファイルをアップロードすると、Gemini APIで文字起こし・要約を行い、
クライアントごとのGoogleスプレッドシート(管理表)に自動で書き込むFastAPIアプリです。

## 構成

```
.
├── shiryou_soufu_multi_client.py   # FastAPIアプリ本体 + バックグラウンドワーカー
├── templates/
│   └── upload_form.html            # 音声アップロードフォーム(Jinja2)
├── requirements.txt
└── Procfile                        # Railway/Heroku用起動コマンド
```

- アップロードは受付のみ同期処理し、即座にレスポンスを返す
- Gemini処理・シート書き込みはAPScheduler製バックグラウンドワーカーが15秒間隔で処理
- どのクライアントのどの管理表に書き込むかは `client_sheets` テーブルで管理(コード変更不要)

## 必要な環境変数

| 変数名 | 必須 | 説明 |
|---|---|---|
| `GEMINI_API_KEY` | ○ | Google AI Studioで発行するGemini APIキー |
| `DATABASE_URL` | ○ | PostgreSQL接続文字列。例: `postgresql://user:pass@host:5432/dbname`(RailwayでPostgreSQLプラグインを追加すると自動発行される) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ○(Railway等) | Google Sheets APIサービスアカウントの秘密鍵JSONの中身をそのまま文字列で設定(ファイルアップロードが困難な環境向け) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | 任意 | ローカル実行時、JSONファイルのパスで認証する場合に使用(デフォルト: `service_account.json`)。`GOOGLE_SERVICE_ACCOUNT_JSON` が設定されている場合はそちらが優先される |
| `UPLOAD_DIR` | 任意 | 音声一時保存先ディレクトリ(デフォルト: `/tmp/shiryou_soufu_uploads`) |

サービスアカウントには、書き込み対象のGoogleスプレッドシートを「編集者」として共有しておく必要があります。

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

起動後、`http://127.0.0.1:8000/shiryou-soufu/{client_code}/upload` にアクセスするとアップロードフォームが表示されます(`client_code` は事前に `client_sheets` テーブルへ登録が必要)。

初回起動時に `client_sheets` / `audio_jobs` テーブルが自動作成されます。

## Railwayへのデプロイ手順

1. **GitHub連携**
   - Railwayダッシュボードで `New Project` → `Deploy from GitHub repo` を選択
   - このリポジトリ(`shiryou-soufu-automation`)を選択してデプロイ
2. **PostgreSQL追加**
   - プロジェクト内で `New` → `Database` → `Add PostgreSQL` を選択
   - 追加すると `DATABASE_URL` が自動生成され、同プロジェクト内の他サービスから参照可能になる
3. **環境変数設定**
   - デプロイしたWebサービスの `Variables` タブで以下を設定
     - `GEMINI_API_KEY`
     - `DATABASE_URL` (PostgreSQLサービスの変数を参照する場合は `${{Postgres.DATABASE_URL}}` のようにReference可能)
     - `GOOGLE_SERVICE_ACCOUNT_JSON`(サービスアカウントJSONファイルの中身をそのまま貼り付け)
4. **Generate Domain**
   - Webサービスの `Settings` → `Networking` → `Generate Domain` で公開URLを発行
   - 発行されたURL + `/shiryou-soufu/{client_code}/upload` を各クライアント担当者に共有

## 新規クライアントの登録方法

Railwayの PostgreSQL サービスに接続し(`Data` タブの `Query` から、またはローカルから `psql $DATABASE_URL` で接続)、`client_sheets` テーブルに1行INSERTするだけで新しいクライアントを追加できます。

```sql
INSERT INTO client_sheets (client_code, display_name, spreadsheet_url)
VALUES (
  'acme',
  '株式会社Acme',
  'https://docs.google.com/spreadsheets/d/xxxxxxxxxxxxxxxx/edit?gid=123456#gid=123456'
);
```

- `client_code`: URLパスに使う識別子(例: `acme` → `/shiryou-soufu/acme/upload`)。半角英数字推奨
- `display_name`: フォームやSlack通知に表示される名前
- `spreadsheet_url`: 管理表のスプレッドシートURL。`gid` 付きURLをコピペすればそのタブが直接使われる。`gid` が無い場合はタブ名に「資料送付」を含むシートを自動検索する
- 列(電話番号列・詳細列)はヘッダー行の文字列から自動特定されるため、個別設定は不要
- サービスアカウントのメールアドレスを、対象スプレッドシートの共有設定に「編集者」として追加しておくこと

## Slack通知の接続

`notify_slack_success` / `notify_slack_no_match` / `notify_slack_failure` (`shiryou_soufu_multi_client.py` 内)は現状ログ出力のみのスタブです。既存のSlack連携がある場合はこの3関数の中身を差し替えてください。
