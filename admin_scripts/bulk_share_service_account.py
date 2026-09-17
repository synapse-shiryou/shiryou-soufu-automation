"""
資料送付報告自動化 - サービスアカウント一括共有スクリプト

jdnet_customer@writeup.co.jp でOAuth認証し、そのアカウントがアクセスできる
全Googleスプレッドシートのうち、タブ名に「資料送付」を含むものだけを対象に、
サービスアカウントを編集者として一括追加する。

使い方:
  cd admin_scripts
  pip install -r requirements.txt
  python bulk_share_service_account.py

初回実行時、ブラウザが開いて jdnet_customer@writeup.co.jp でのログイン・許可を求められる。
"""

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCRIPT_DIR = Path(__file__).parent
CLIENT_SECRET_FILE = SCRIPT_DIR / "client_secret.json"
TOKEN_FILE = SCRIPT_DIR / "token.json"
PROGRESS_FILE = SCRIPT_DIR / "bulk_share_progress.jsonl"

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

# アプリ本体(shiryou_soufu_multi_client.py)と同じ判定基準
TAB_NAME_KEYWORD = "資料送付"

SERVICE_ACCOUNT_EMAIL = "shiryou-soufu-sheets@sturdy-block-508908-d7.iam.gserviceaccount.com"


def get_credentials() -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return creds


def list_all_spreadsheets(drive):
    """認証ユーザーがアクセスできる全スプレッドシートを列挙する(自分が作成したものだけでなく、共有されたものも含む)。"""
    files = []
    page_token = None
    query = "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    while True:
        response = (
            drive.files()
            .list(
                q=query,
                spaces="drive",
                corpora="allDrives",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields="nextPageToken, files(id, name, capabilities/canShare)",
                pageToken=page_token,
                pageSize=200,
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def has_matching_tab(sheets, spreadsheet_id: str) -> bool:
    try:
        meta = (
            sheets.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
            .execute()
        )
    except HttpError as e:
        print(f"  [警告] タブ情報取得失敗: {e}")
        return False
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    return any(TAB_NAME_KEYWORD in t for t in titles)


def already_shared(drive, file_id: str) -> bool:
    permissions = (
        drive.permissions()
        .list(fileId=file_id, fields="permissions(emailAddress)", supportsAllDrives=True)
        .execute()
        .get("permissions", [])
    )
    return any(p.get("emailAddress") == SERVICE_ACCOUNT_EMAIL for p in permissions)


def share_with_service_account(drive, file_id: str):
    drive.permissions().create(
        fileId=file_id,
        body={"type": "user", "role": "writer", "emailAddress": SERVICE_ACCOUNT_EMAIL},
        sendNotificationEmail=False,
        supportsAllDrives=True,
    ).execute()


def load_progress():
    """前回までに処理済みのfile_idを読み込む(中断からの再開用)。"""
    done = {}
    if PROGRESS_FILE.exists():
        for line in PROGRESS_FILE.read_text().splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            done[entry["id"]] = entry
    return done


def append_progress(entry: dict):
    with PROGRESS_FILE.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main():
    creds = get_credentials()
    drive = build("drive", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    print("スプレッドシート一覧を取得中...")
    all_files = list_all_spreadsheets(drive)
    print(f"{len(all_files)} 件のスプレッドシートが見つかりました。「{TAB_NAME_KEYWORD}」タブを検索します。\n")

    done = load_progress()
    if done:
        print(f"前回の続きから再開します({len(done)} 件は処理済みのためスキップ)。\n")

    matched, shared, already, failed = [], [], [], []

    for f in all_files:
        file_id, name = f["id"], f["name"]

        if file_id in done:
            entry = done[file_id]
            status = entry["status"]
            if status == "shared":
                shared.append((file_id, name))
                matched.append((file_id, name))
            elif status == "already_shared":
                already.append((file_id, name))
                matched.append((file_id, name))
            elif status == "failed":
                failed.append((file_id, name, entry.get("reason", "")))
                matched.append((file_id, name))
            continue

        try:
            if not has_matching_tab(sheets, file_id):
                append_progress({"id": file_id, "name": name, "status": "not_matched"})
                continue
        except Exception as e:
            append_progress({"id": file_id, "name": name, "status": "failed", "reason": f"タブ確認失敗: {e}"})
            failed.append((file_id, name, f"タブ確認失敗: {e}"))
            matched.append((file_id, name))
            print(f"[対象?] {name}\n  -> [失敗] タブ確認エラー: {e}")
            continue

        matched.append((file_id, name))
        print(f"[対象] {name}")

        try:
            if already_shared(drive, file_id):
                already.append((file_id, name))
                append_progress({"id": file_id, "name": name, "status": "already_shared"})
                print("  -> 既に共有済み")
                continue

            if not f.get("capabilities", {}).get("canShare", True):
                reason = "共有権限なし"
                failed.append((file_id, name, reason))
                append_progress({"id": file_id, "name": name, "status": "failed", "reason": reason})
                print(f"  -> [失敗] {reason}")
                continue

            share_with_service_account(drive, file_id)
            shared.append((file_id, name))
            append_progress({"id": file_id, "name": name, "status": "shared"})
            print("  -> 共有完了")
        except HttpError as e:
            failed.append((file_id, name, str(e)))
            append_progress({"id": file_id, "name": name, "status": "failed", "reason": str(e)})
            print(f"  -> [失敗] {e}")

    print("\n" + "=" * 60)
    print(f"対象ファイル数: {len(matched)}")
    print(f"新規共有: {len(shared)}")
    print(f"既に共有済み: {len(already)}")
    print(f"失敗: {len(failed)}")
    if failed:
        print("\n--- 失敗一覧(手動対応が必要) ---")
        for file_id, name, reason in failed:
            print(f"- {name} (https://docs.google.com/spreadsheets/d/{file_id}/edit) : {reason}")

    report_path = SCRIPT_DIR / "bulk_share_report.json"
    report_path.write_text(
        json.dumps(
            {
                "matched": [{"id": i, "name": n} for i, n in matched],
                "shared": [{"id": i, "name": n} for i, n in shared],
                "already_shared": [{"id": i, "name": n} for i, n in already],
                "failed": [{"id": i, "name": n, "reason": r} for i, n, r in failed],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"\n詳細レポートを保存しました: {report_path}")


if __name__ == "__main__":
    main()
