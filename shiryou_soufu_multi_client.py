"""
資料送付報告 自動化 - 単一URL / 非同期キュー版

想定規模: 管理表(クライアント)約100件、利用者(架電担当)約40名

設計方針:
- アップロードは「受付」だけを同期で行い、即座にレスポンスを返す
  (Gemini処理・シート書き込みはバックグラウンドワーカーに任せる)
- クライアントの事前登録は行わない。どの管理表に書くかは、
  アップロード画面でその都度貼り付けるスプレッドシートURLだけで決まる
- タブ(gid)や列番号は固定値をDBに持たず、実行時に自動で見つける
  - gid付きURLならそのタブを直接開く。gidが無ければタブ名に「資料送付」を
    含むシートを自動検索する
  - 列も番号固定にせず、ヘッダー行の文字列(「電話番号」「詳細」等)から
    自動特定する。100ファイルで列位置がズレていても個別設定は不要
- ワーカーは PostgreSQL の SELECT ... FOR UPDATE SKIP LOCKED で
  ジョブを取り合うので、ワーカーを複数プロセス/複数台に増やしても安全
- Gemini/Sheets 両APIのレート制限を考慮し、同時実行数を絞って処理する
- 完了通知はWebページで待たせず、Slackワークフローに飛ばす

必要パッケージ:
  pip install fastapi uvicorn python-multipart jinja2 sqlalchemy psycopg2-binary \
              apscheduler google-generativeai gspread google-auth requests --break-system-packages

このファイルと同じ階層に templates/upload_form.html を置いてください
(Jinja2Templates(directory="templates") が参照します)。

DB接続文字列は環境変数 DATABASE_URL (例: postgresql://user:pass@host:5432/dbname)

アップロードURLは常に /shiryou-soufu/upload の1つだけです。
"""

import os
import re
import time
import uuid
import logging
from pathlib import Path
from datetime import datetime

import google.generativeai as genai
import gspread
import requests
from google.oauth2.service_account import Credentials
from fastapi import FastAPI, APIRouter, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shiryou_soufu_multi")

# ------------------------------------------------------------------
# 設定
# ------------------------------------------------------------------
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = "gemini-3.6-flash"
DATABASE_URL = os.environ["DATABASE_URL"]
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/tmp/shiryou_soufu_uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Slackワークフロー「資料送付報告_v2」のWebhookトリガーURL。
# 書き込み成功時に、案件名とスプレッドシートURLを渡して起動する。未設定なら何もしない。
SLACK_WORKFLOW_WEBHOOK_URL = os.environ.get("SLACK_WORKFLOW_WEBHOOK_URL")

# 40名が同時に投げても詰まらないよう、ワーカーが一度に処理する件数を絞る。
# Geminiの契約プラン(RPM上限)に合わせて調整してください。
MAX_JOBS_PER_TICK = 5
WORKER_INTERVAL_SECONDS = 15

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

PROMPT = """役割
あなたは、営業架電の音声文字起こしまたは架電メモから、Slack共有用の「資料送付報告」を作成するアシスタントです。
資料送付報告では、管理者が確認する項目を増やさないため、接触先、電話番号、メールアドレス、部署名、役職などは出力しないでください。
入力データをもとに、以下のFMTだけで出力してください。
絶対ルール
・出力FMTの見出し、項目名、順番は絶対に変更しないでください
・FMTにない項目は絶対に追加しないでください
・前置き、説明、分析コメント、注意書きは一切出力しないでください
・接触先、電話番号、メールアドレス、部署名、役職は出力しないでください
・入力データにない情報は「不明」と記載してください
・入力データにない情報を推測で作らないでください
・音声の言い間違い、言い淀み、重複表現は整理してください
・相手の発言をそのまま長く引用せず、営業共有用に短く要約してください
・NG理由、断り文句、不要な雑談は一切含めないでください
・資料送付に至った理由、相手の受け止め方、次回の再接触に使える情報だけを残してください
・全体は短い箇条書きで、見ただけでわかる状態にしてください
出力FMT
【資料送付報告】
企業名：
【共有した内容】
・伝えたメリットや数字：
【相手の意思・理解】
・先方の現状や受け止め方：
"""

# ------------------------------------------------------------------
# DBスキーマ (初回起動時に作成)
# ------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audio_jobs (
    id              SERIAL PRIMARY KEY,
    phone_number    TEXT NOT NULL,
    uploader        TEXT,
    file_path       TEXT NOT NULL,
    spreadsheet_url TEXT NOT NULL,  -- アップロード画面で毎回指定されるスプレッドシートURL
    kakudo          TEXT,  -- 確度: 高/中/低
    staff_name      TEXT,  -- 実施した架電担当者(staff_members.nameを選択)
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending/processing/done/no_match/error
    result_text     TEXT,
    error_message   TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT now(),
    updated_at      TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS staff_members (
    name           TEXT PRIMARY KEY,
    slack_user_id  TEXT NOT NULL
);

-- 旧バージョン(クライアント事前登録制)からの移行
ALTER TABLE audio_jobs DROP CONSTRAINT IF EXISTS audio_jobs_client_code_fkey;
ALTER TABLE audio_jobs DROP COLUMN IF EXISTS client_code;
ALTER TABLE audio_jobs ADD COLUMN IF NOT EXISTS spreadsheet_url TEXT;
ALTER TABLE audio_jobs ADD COLUMN IF NOT EXISTS kakudo TEXT;
ALTER TABLE audio_jobs ADD COLUMN IF NOT EXISTS staff_name TEXT;
DROP TABLE IF EXISTS client_sheets;
"""


def init_db():
    with engine.begin() as conn:
        conn.execute(text(SCHEMA_SQL))


# ------------------------------------------------------------------
# Gemini / Sheets 呼び出し
# ------------------------------------------------------------------
def transcribe_and_summarize(audio_path: str) -> str:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL, system_instruction=PROMPT)

    audio_file = genai.upload_file(path=audio_path)
    while audio_file.state.name == "PROCESSING":
        time.sleep(2)
        audio_file = genai.get_file(audio_file.name)

    if audio_file.state.name == "FAILED":
        raise RuntimeError(f"Gemini file upload failed: {audio_file.name}")

    response = model.generate_content(
        [audio_file, "この音声をもとに、指示されたFMTで資料送付報告を作成してください。"]
    )
    return response.text.strip()


_gspread_client = None


def get_gspread_client():
    """
    サービスアカウント認証情報の読み込み。
    - ローカル/自前サーバーなら GOOGLE_SERVICE_ACCOUNT_FILE (JSONファイルパス) を使用
    - Railway/RenderなどファイルUPが手間な環境では、JSON中身をそのまま
      環境変数 GOOGLE_SERVICE_ACCOUNT_JSON に入れておけばそちらを使う
    """
    global _gspread_client
    if _gspread_client is None:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if service_account_json:
            import json
            info = json.loads(service_account_json)
            creds = Credentials.from_service_account_info(info, scopes=scopes)
        else:
            creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
        _gspread_client = gspread.authorize(creds)
    return _gspread_client


# タブ名の自動検索キーワード(この文字を含むタブを「資料送付管理表」とみなす)
TAB_NAME_KEYWORD = "資料送付"

# ヘッダー行から列を特定するための候補文字列(複数書けば表記ゆれに対応できる)
PHONE_HEADER_CANDIDATES = ["電話番号", "TEL", "電話"]
DETAIL_HEADER_CANDIDATES = ["詳細", "商談結果詳細記述", "架電メモ", "資料送付報告"]
HEADER_SEARCH_ROWS = 3  # ヘッダーが1行目にない場合に備えて数行だけ探す

# 確度の書き込み先は固定でM列
KAKUDO_COLUMN = 13  # A=1, ..., M=13
KAKUDO_CHOICES = ["高", "中", "低"]


def parse_spreadsheet_url(url: str) -> tuple[str, int | None]:
    """スプレッドシートURLから (spreadsheet_id, gid) を取り出す。gidが無ければNone。"""
    id_match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not id_match:
        # URLでなくID単体で登録された場合にも対応
        return url.strip(), None
    spreadsheet_id = id_match.group(1)
    gid_match = re.search(r"[?&#]gid=(\d+)", url)
    gid = int(gid_match.group(1)) if gid_match else None
    return spreadsheet_id, gid


def resolve_worksheet(spreadsheet_url: str):
    """URLからスプレッドシート・タブを自動解決する。

    URLのgidは、タブを切り替えた状態でコピーすると意図しないタブを指してしまいがちなので
    信用しすぎない。常にタブ名に TAB_NAME_KEYWORD を含むタブを優先的に探し、
    複数該当した場合だけgidで絞り込む。該当タブが無い場合のみgidにフォールバックする。
    """
    spreadsheet_id, gid = parse_spreadsheet_url(spreadsheet_url)
    sh = get_gspread_client().open_by_key(spreadsheet_id)

    candidates = [ws for ws in sh.worksheets() if TAB_NAME_KEYWORD in ws.title]

    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1:
        if gid is not None:
            for ws in candidates:
                if ws.id == gid:
                    return ws
        raise RuntimeError(
            f"「{TAB_NAME_KEYWORD}」を含むタブが複数見つかりました({[w.title for w in candidates]})。"
            "書き込みたいタブを開いた状態のURL(gid付き)を貼り直してください。"
        )

    # 「資料送付」を含むタブが無ければ、gid指定を最後の手段として使う
    if gid is not None:
        for ws in sh.worksheets():
            if ws.id == gid:
                return ws
        raise RuntimeError(f"gid={gid} のタブが見つかりません: {spreadsheet_id}")

    raise RuntimeError(f"「{TAB_NAME_KEYWORD}」を含むタブが見つかりません: {spreadsheet_id}")


def find_header_columns(ws) -> tuple[int, int]:
    """ヘッダー行を数行スキャンして、電話番号列・詳細列の列番号を自動特定する"""
    all_values = ws.get_values(f"A1:ZZ{HEADER_SEARCH_ROWS}")
    phone_col = detail_col = None

    for row_values in all_values:
        for idx, cell_value in enumerate(row_values, start=1):
            if phone_col is None and any(c in cell_value for c in PHONE_HEADER_CANDIDATES):
                phone_col = idx
            if detail_col is None and any(c in cell_value for c in DETAIL_HEADER_CANDIDATES):
                detail_col = idx
        if phone_col and detail_col:
            break

    if phone_col is None or detail_col is None:
        raise RuntimeError(
            f"ヘッダーから列を特定できませんでした(phone_col={phone_col}, detail_col={detail_col})。"
            "見出し文字列の候補(PHONE_HEADER_CANDIDATES/DETAIL_HEADER_CANDIDATES)を見直してください。"
        )
    return phone_col, detail_col


def find_row_by_phone(ws, col_phone: int, phone_number: str) -> int | None:
    normalized_target = re.sub(r"\D", "", phone_number)
    if not normalized_target:
        return None
    for i, val in enumerate(ws.col_values(col_phone), start=1):
        if re.sub(r"\D", "", val) == normalized_target:
            return i
    return None


def write_with_retry(ws, row: int, col: int, text_value: str, max_retries: int = 3):
    """Sheets APIのレート制限(429)対策で指数バックオフ付きリトライ"""
    for attempt in range(max_retries):
        try:
            ws.update_cell(row, col, text_value)
            return
        except gspread.exceptions.APIError as e:
            wait = 2 ** attempt
            logger.warning("Sheets API エラー、%d秒待って再試行: %s", wait, e)
            time.sleep(wait)
    raise RuntimeError("Sheets APIへの書き込みが規定回数失敗しました")


# ------------------------------------------------------------------
# ジョブ処理 (ワーカー本体)
# ------------------------------------------------------------------
def process_pending_jobs():
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, phone_number, file_path, spreadsheet_url, kakudo, staff_name
                FROM audio_jobs
                WHERE status = 'pending'
                ORDER BY created_at
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
                """
            ),
            {"limit": MAX_JOBS_PER_TICK},
        ).fetchall()

        for job_id in [r.id for r in rows]:
            conn.execute(
                text("UPDATE audio_jobs SET status='processing', updated_at=now() WHERE id=:id"),
                {"id": job_id},
            )

    for row in rows:
        _process_single_job(row.id, row.phone_number, row.file_path, row.spreadsheet_url, row.kakudo, row.staff_name)


def _process_single_job(
    job_id: int,
    phone_number: str,
    file_path: str,
    spreadsheet_url: str,
    kakudo: str | None = None,
    staff_name: str | None = None,
):
    try:
        summary_text = transcribe_and_summarize(file_path)
    except Exception as e:
        logger.exception("job=%d Gemini処理失敗", job_id)
        _update_job(job_id, "error", error_message=str(e))
        notify_slack_failure(job_id, phone_number)
        return
    finally:
        try:
            os.remove(file_path)
        except OSError:
            pass

    try:
        ws = resolve_worksheet(spreadsheet_url)
        phone_col, detail_col = find_header_columns(ws)
        row_num = find_row_by_phone(ws, phone_col, phone_number)

        if row_num is None:
            _update_job(job_id, "no_match", result_text=summary_text)
            notify_slack_no_match(job_id, phone_number, summary_text)
            return

        write_with_retry(ws, row_num, detail_col, summary_text)
        if kakudo:
            write_with_retry(ws, row_num, KAKUDO_COLUMN, kakudo)
    except Exception as e:
        logger.exception("job=%d シート書き込み失敗", job_id)
        _update_job(job_id, "error", error_message=str(e), result_text=summary_text)
        notify_slack_failure(job_id, phone_number)
        return

    _update_job(job_id, "done", result_text=summary_text)
    notify_slack_success(job_id, row_num, summary_text)

    # Slack ワークフロー側で「表示名」形式のSlackユーザーID変数として解決させるため、
    # <@...>で囲まず生のユーザーIDを渡す。見つからない場合は入力名をそのまま渡す。
    staff_slack_user_id = _get_staff_slack_user_id(staff_name) if staff_name else None
    uploader_mention = staff_slack_user_id or (staff_name or "")
    trigger_shiryou_soufu_workflow(
        official_deal_name=ws.spreadsheet.title,
        spreadsheet_url=spreadsheet_url,
        uploader_mention=uploader_mention,
    )


def _update_job(job_id: int, status: str, result_text: str | None = None, error_message: str | None = None):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE audio_jobs
                SET status=:status, result_text=:result_text,
                    error_message=:error_message, updated_at=now()
                WHERE id=:id
                """
            ),
            {"id": job_id, "status": status, "result_text": result_text, "error_message": error_message},
        )


# ------------------------------------------------------------------
# Slack通知 (既存のSlack連携関数に差し替えてください)
# ------------------------------------------------------------------
def notify_slack_success(job_id, row_num, summary_text):
    logger.info("[Slack成功通知] %d行目更新 job=%d\n%s", row_num, job_id, summary_text[:80])


def notify_slack_no_match(job_id, phone_number, summary_text):
    logger.info("[Slack要確認通知] 電話番号%s の行が見つかりません job=%d", phone_number, job_id)


def notify_slack_failure(job_id, phone_number):
    logger.info("[Slack失敗通知] phone=%s job=%d", phone_number, job_id)


def trigger_shiryou_soufu_workflow(official_deal_name: str, spreadsheet_url: str, uploader_mention: str = ""):
    """Slackワークフロー「資料送付報告_v2」のWebhookトリガーを起動する。"""
    if not SLACK_WORKFLOW_WEBHOOK_URL:
        logger.info("[Slackワークフロー] SLACK_WORKFLOW_WEBHOOK_URL未設定のためスキップ")
        return
    try:
        response = requests.post(
            SLACK_WORKFLOW_WEBHOOK_URL,
            json={
                "official_deal_name": official_deal_name,
                "spreadsheet_url": spreadsheet_url,
                "uploader_mention": uploader_mention,
            },
            timeout=10,
        )
        response.raise_for_status()
        logger.info(
            "[Slackワークフロー] 起動成功 official_deal_name=%s status=%s",
            official_deal_name, response.status_code,
        )
    except requests.RequestException:
        logger.exception("[Slackワークフロー] 起動失敗 official_deal_name=%s", official_deal_name)


# ------------------------------------------------------------------
# FastAPI
# ------------------------------------------------------------------
router = APIRouter(prefix="/shiryou-soufu", tags=["shiryou-soufu"])
templates = Jinja2Templates(directory="templates")  # templates/upload_form.html を配置してください


def _get_staff_names() -> list[str]:
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT name FROM staff_members ORDER BY name")).fetchall()
    return [r.name for r in rows]


def _get_staff_slack_user_id(staff_name: str) -> str | None:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT slack_user_id FROM staff_members WHERE name=:n"),
            {"n": staff_name},
        ).fetchone()
    return row.slack_user_id if row else None


@router.get("/upload", response_class=HTMLResponse)
async def show_upload_form(request: Request):
    return templates.TemplateResponse(
        request,
        "upload_form.html",
        {
            "result_status": None,
            "staff_members": _get_staff_names(),
        },
    )


@router.post("/upload", response_class=HTMLResponse)
async def handle_upload(
    request: Request,
    phone_number: str = Form(...),
    uploader: str = Form(""),
    spreadsheet_url: str = Form(...),
    kakudo: str = Form(...),
    staff_name: str = Form(...),
    audio_file: UploadFile = File(...),
):
    if not spreadsheet_url.strip():
        raise HTTPException(status_code=400, detail="スプレッドシートURLを入力してください")

    if kakudo not in KAKUDO_CHOICES:
        raise HTTPException(status_code=400, detail=f"確度は{KAKUDO_CHOICES}のいずれかを選択してください")

    if staff_name not in _get_staff_names():
        raise HTTPException(status_code=400, detail="実施者を選択してください")

    suffix = Path(audio_file.filename).suffix or ".mp3"
    saved_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    with open(saved_path, "wb") as f:
        f.write(await audio_file.read())

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO audio_jobs (phone_number, uploader, file_path, spreadsheet_url, kakudo, staff_name)
                VALUES (:phone_number, :uploader, :file_path, :spreadsheet_url, :kakudo, :staff_name)
                """
            ),
            {
                "phone_number": phone_number,
                "uploader": uploader,
                "file_path": str(saved_path),
                "spreadsheet_url": spreadsheet_url.strip(),
                "kakudo": kakudo,
                "staff_name": staff_name,
            },
        )

    return templates.TemplateResponse(
        request,
        "upload_form.html",
        {
            "result_status": "accepted",
            "staff_members": _get_staff_names(),
        },
    )


# ------------------------------------------------------------------
# アプリ起動
# ------------------------------------------------------------------
app = FastAPI(title="資料送付報告 自動作成(マルチクライアント)")
app.include_router(router)

scheduler = BackgroundScheduler()


@app.on_event("startup")
def on_startup():
    init_db()
    scheduler.add_job(process_pending_jobs, "interval", seconds=WORKER_INTERVAL_SECONDS)
    scheduler.start()
    logger.info("ワーカーを起動しました (interval=%ds, batch=%d)", WORKER_INTERVAL_SECONDS, MAX_JOBS_PER_TICK)


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
