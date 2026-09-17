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
              apscheduler gspread google-auth requests --break-system-packages

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
from datetime import datetime, date

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
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_TRANSCRIBE_MODEL = "whisper-large-v3"
GROQ_CHAT_MODEL = "openai/gpt-oss-120b"
DATABASE_URL = os.environ["DATABASE_URL"]
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/tmp/shiryou_soufu_uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Slackワークフロー「資料送付報告_v2」のWebhookトリガーURL。
# 書き込み成功時に、案件名とスプレッドシートURLを渡して起動する。未設定なら何もしない。
SLACK_WORKFLOW_WEBHOOK_URL = os.environ.get("SLACK_WORKFLOW_WEBHOOK_URL")
SLACK_WORKFLOW_WEBHOOK_URL_APO = os.environ.get("SLACK_WORKFLOW_WEBHOOK_URL_APO")

# 実施者一覧を毎日自動同期するためのSlack Bot Token(channels:read, users:read)。
# 未設定の場合は同期処理自体をスキップする(手動登録のみで運用可能)。
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
STAFF_SYNC_CHANNEL_ID = os.environ.get("STAFF_SYNC_CHANNEL_ID", "C0B87GX6RCG")  # #13_全体連絡チャンネル
STAFF_SYNC_INTERVAL_HOURS = 24

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

PROMPT_APO = """役割
あなたは、営業架電の音声文字起こしまたは架電メモから、Slack共有用の「アポ獲得報告」を作成するアシスタントです。
入力データをもとに、以下のFMTだけで出力してください。
絶対ルール
・出力FMTの見出し、項目名、順番は絶対に変更しないでください
・FMTにない項目は絶対に追加しないでください
・前置き、説明、分析コメント、注意書きは一切出力しないでください
・入力データにない情報は「不明」と記載してください
・入力データにない情報を推測で作らないでください
・音声の言い間違い、言い淀み、重複表現は整理してください
・相手の発言をそのまま長く引用せず、営業共有用に短く要約してください
・NG理由、断り文句、不要な雑談は一切含めないでください
・数字、日時、企業名、担当者名は特に正確に抽出してください
・「人柄・印象」は、発言内容・話し方・態度など、入力データから客観的に読み取れる範囲のみ記載してください。判断できない場合は「不明」と記載してください
・「性別」は、名前・呼称・話し方などから明確に判断できる場合のみ記載し、判断できない場合は「不明」と記載してください
・「先方の興味がどのくらいの確度なのか」は、高 / 中 / 低 のどれか一つだけを選んでください
・確度の理由は、相手の反応、日程確定度、課題感、検討意欲をもとに簡潔に書いてください
・「ニーズ属性」は、潜在ニーズ / 顕在ニーズ のどちらか一つだけを選んでください
・ニーズ属性の理由は、相手が課題を自覚しているか、具体的な悩みを話しているか、検討の緊急度などをもとに簡潔に書いてください
・全体は短い箇条書きで、見ただけでわかる状態にしてください
出力FMT

【アポ獲得報告】
①担当者情報
担当者名：
部署名・役職：
人柄・印象：
性別：

②商談方法
オンライン / 電話 / 訪問：

③共有した内容
伝えたメリット：

④相手の意思・理解
先方の現状や受け止め方：

⑤どこに興味を持っているか
一番食いつきが良かった部分：

⑥ 先方の興味・角度
先方の興味がどのくらいの確度なのか：高 / 中 / 低
理由：

⑦ ニーズ属性
潜在ニーズ / 顕在ニーズ：
理由：

⑩ 補足：
"""

REPORT_TYPE_CHOICES = ["資料送付", "アポ獲得"]
REPORT_TYPE_TAB_KEYWORDS = {
    "資料送付": "資料送付",
    "アポ獲得": "商談アポ",
}
REPORT_TYPE_PROMPTS = {
    "資料送付": PROMPT,
    "アポ獲得": PROMPT_APO,
}

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
    kakudo          TEXT,  -- 確度: 高/中/低(資料送付のみ)
    chakuden_saki   TEXT,  -- 着電先: 受付/担当者/代表(資料送付のみ)
    report_type     TEXT NOT NULL DEFAULT '資料送付',  -- 報告種別: 資料送付/アポ獲得
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
ALTER TABLE audio_jobs ADD COLUMN IF NOT EXISTS chakuden_saki TEXT;
ALTER TABLE audio_jobs ADD COLUMN IF NOT EXISTS report_type TEXT NOT NULL DEFAULT '資料送付';
DROP TABLE IF EXISTS client_sheets;
"""


def init_db():
    with engine.begin() as conn:
        conn.execute(text(SCHEMA_SQL))


# ------------------------------------------------------------------
# Gemini / Sheets 呼び出し
# ------------------------------------------------------------------
def transcribe_and_summarize(audio_path: str, prompt: str = PROMPT) -> str:
    """Groq APIで音声を文字起こしし(Whisper)、続けて要約(Llama)する。"""
    with open(audio_path, "rb") as f:
        transcribe_resp = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": (os.path.basename(audio_path), f)},
            data={"model": GROQ_TRANSCRIBE_MODEL, "language": "ja"},
            timeout=120,
        )
    transcribe_resp.raise_for_status()
    transcript = transcribe_resp.json()["text"]

    chat_resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": GROQ_CHAT_MODEL,
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": f"以下は営業架電音声の文字起こしです。指示されたFMTで報告を作成してください。\n\n{transcript}",
                },
            ],
        },
        timeout=60,
    )
    chat_resp.raise_for_status()
    return chat_resp.json()["choices"][0]["message"]["content"].strip()


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

# 電話番号が「資料送付」タブに無かった場合に参照する、全リード一覧タブの検索キーワード
REFERENCE_TAB_KEYWORD = "営業リスト"
REFERENCE_TAB_EXCLUDE_KEYWORDS = ["NG"]  # 「営業NGリスト」などは除外

# ヘッダー行から列を特定するための候補文字列(複数書けば表記ゆれに対応できる)
PHONE_HEADER_CANDIDATES = ["電話番号", "TEL", "電話"]
DETAIL_HEADER_CANDIDATES = ["詳細", "商談結果詳細記述", "架電メモ", "資料送付報告", "ヒアリング内容"]
KAKUDO_HEADER_CANDIDATES = ["確度", "見込み"]
HEADER_SEARCH_ROWS = 3  # ヘッダーが1行目にない場合に備えて数行だけ探す

# 「資料送付」タブに新規行を追加する際に埋める項目の候補列(無ければその項目はスキップされる)
TARGET_COMPANY_HEADER_CANDIDATES = ["会社名", "企業名"]
TARGET_URL_HEADER_CANDIDATES = ["URL", "HP", "ホームページ"]
TARGET_CONTACT_HEADER_CANDIDATES = ["着電先", "役職"]
TARGET_LASTNAME_HEADER_CANDIDATES = ["姓"]
TARGET_FIRSTNAME_HEADER_CANDIDATES = ["名"]
TARGET_EMAIL_HEADER_CANDIDATES = ["メールアドレス"]
TARGET_ACQUIRE_DATE_HEADER_CANDIDATES = ["取得日"]

# 「営業リスト」タブ側で、上記項目を引くための候補列
REF_COMPANY_HEADER_CANDIDATES = ["企業名", "会社名"]
REF_URL_HEADER_CANDIDATES = ["HP", "URL", "ホームページ"]
REF_CONTACT_HEADER_CANDIDATES = ["着電先", "役職"]
REF_LASTNAME_HEADER_CANDIDATES = ["姓"]
REF_FIRSTNAME_HEADER_CANDIDATES = ["名"]
REF_EMAIL_HEADER_CANDIDATES = ["メールアドレス"]

KAKUDO_CHOICES = ["高", "中", "低"]
CHAKUDEN_SAKI_CHOICES = ["受付", "担当者", "代表"]


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


def resolve_worksheet(spreadsheet_url: str, tab_keyword: str = TAB_NAME_KEYWORD):
    """URLからスプレッドシート・タブを自動解決する。

    URLのgidは、タブを切り替えた状態でコピーすると意図しないタブを指してしまいがちなので
    信用しすぎない。常にタブ名に tab_keyword を含むタブを優先的に探し、
    複数該当した場合だけgidで絞り込む。該当タブが無い場合のみgidにフォールバックする。
    """
    spreadsheet_id, gid = parse_spreadsheet_url(spreadsheet_url)
    sh = get_gspread_client().open_by_key(spreadsheet_id)

    candidates = [ws for ws in sh.worksheets() if tab_keyword in ws.title]

    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1:
        if gid is not None:
            for ws in candidates:
                if ws.id == gid:
                    return ws
        raise RuntimeError(
            f"「{tab_keyword}」を含むタブが複数見つかりました({[w.title for w in candidates]})。"
            "書き込みたいタブを開いた状態のURL(gid付き)を貼り直してください。"
        )

    # 対象キーワードを含むタブが無ければ、gid指定を最後の手段として使う
    if gid is not None:
        for ws in sh.worksheets():
            if ws.id == gid:
                return ws
        raise RuntimeError(f"gid={gid} のタブが見つかりません: {spreadsheet_id}")

    raise RuntimeError(f"「{tab_keyword}」を含むタブが見つかりません: {spreadsheet_id}")


def find_header_columns(ws) -> tuple[int, int, int | None]:
    """ヘッダー行を数行スキャンして、電話番号列・詳細列・確度列の列番号を自動特定する

    確度列は無くても書き込み自体は継続できるので None を許容する。
    同じ列が複数の役割に重複マッチした場合は、後勝ちで上書きされる事故を防ぐため
    それぞれ別列として検出できたものだけを採用する。
    """
    all_values = ws.get_values(f"A1:ZZ{HEADER_SEARCH_ROWS}")
    phone_col = detail_col = kakudo_col = None

    for row_values in all_values:
        for idx, cell_value in enumerate(row_values, start=1):
            if phone_col is None and any(c in cell_value for c in PHONE_HEADER_CANDIDATES):
                phone_col = idx
            if detail_col is None and any(c in cell_value for c in DETAIL_HEADER_CANDIDATES):
                detail_col = idx
            if kakudo_col is None and any(c in cell_value for c in KAKUDO_HEADER_CANDIDATES):
                kakudo_col = idx
        if phone_col and detail_col and kakudo_col:
            break

    if phone_col is None or detail_col is None:
        raise RuntimeError(
            f"ヘッダーから列を特定できませんでした(phone_col={phone_col}, detail_col={detail_col})。"
            "見出し文字列の候補(PHONE_HEADER_CANDIDATES/DETAIL_HEADER_CANDIDATES)を見直してください。"
        )
    if kakudo_col is not None and kakudo_col == detail_col:
        logger.warning(
            "確度列が詳細列と同じ列(%d)に一致したため、確度列は未検出として扱います", kakudo_col
        )
        kakudo_col = None
    return phone_col, detail_col, kakudo_col


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


def _find_column(all_values: list[list[str]], candidates: list[str], exact: bool = False) -> int | None:
    """ヘッダー行群から候補文字列に一致する最初の列番号を返す。

    exact=True の場合はセルの中身が候補文字列と完全一致した場合のみマッチする
    (「姓」「名」のような1文字の候補は部分一致だと別の見出しに誤爆するため)。
    """
    for row_values in all_values:
        for idx, cell_value in enumerate(row_values, start=1):
            cell = cell_value.strip()
            if exact:
                if cell in candidates:
                    return idx
            else:
                if any(c in cell_value for c in candidates):
                    return idx
    return None


def find_reference_worksheet(sh):
    """「資料送付」タブに電話番号が無かった場合に参照する、全リード一覧タブを探す。

    「営業NGリスト」等は REFERENCE_TAB_EXCLUDE_KEYWORDS で除外し、
    完全一致するタブがあればそれを優先、無ければタイトルが最短のものを採用する。
    """
    candidates = [
        ws for ws in sh.worksheets()
        if REFERENCE_TAB_KEYWORD in ws.title
        and not any(ex in ws.title for ex in REFERENCE_TAB_EXCLUDE_KEYWORDS)
    ]
    if not candidates:
        return None
    for ws in candidates:
        if ws.title == REFERENCE_TAB_KEYWORD:
            return ws
    return min(candidates, key=lambda w: len(w.title))


def lookup_reference_row(ref_ws, phone_number: str) -> dict | None:
    """営業リストタブを電話番号で検索し、資料送付タブへコピーする情報を返す。見つからなければNone。"""
    header_values = ref_ws.get_values(f"A1:ZZ{HEADER_SEARCH_ROWS}")
    ref_phone_col = _find_column(header_values, PHONE_HEADER_CANDIDATES)
    if ref_phone_col is None:
        return None

    row_num = find_row_by_phone(ref_ws, ref_phone_col, phone_number)
    if row_num is None:
        return None

    row_values = ref_ws.row_values(row_num)

    def get(col_idx: int | None) -> str:
        if col_idx is None or col_idx > len(row_values):
            return ""
        return row_values[col_idx - 1]

    return {
        "company": get(_find_column(header_values, REF_COMPANY_HEADER_CANDIDATES)),
        "url": get(_find_column(header_values, REF_URL_HEADER_CANDIDATES)),
        "contact": get(_find_column(header_values, REF_CONTACT_HEADER_CANDIDATES)),
        "lastname": get(_find_column(header_values, REF_LASTNAME_HEADER_CANDIDATES)),
        "firstname": get(_find_column(header_values, REF_FIRSTNAME_HEADER_CANDIDATES, exact=True)),
        "email": get(_find_column(header_values, REF_EMAIL_HEADER_CANDIDATES)),
        "phone": get(ref_phone_col),
    }


def _find_next_empty_row(ws) -> int:
    """取得日列の最終入力行を基準に、次に書き込むべき空き行を返す。

    取得日は実データの行なら必ず入っている運用のため、これを基準にすれば
    他の列(A列など)がたまたま空欄でも実データ行を誤って上書きしない。
    取得日列が見つからない場合のみ、行全体(get_all_values)で判定する。
    """
    header_values = ws.get_values(f"A1:ZZ{HEADER_SEARCH_ROWS}")
    acquire_date_col = _find_column(header_values, TARGET_ACQUIRE_DATE_HEADER_CANDIDATES)
    if acquire_date_col is not None:
        return len(ws.col_values(acquire_date_col)) + 1

    all_values = ws.get_all_values()
    for i in range(len(all_values) - 1, -1, -1):
        if any(cell.strip() for cell in all_values[i]):
            return i + 2
    return 2


def append_row_from_reference(ws, phone_col: int, ref_data: dict) -> int:
    """営業リストから引いた情報をもとに、資料送付タブへ新規行を追加して行番号を返す。"""
    header_values = ws.get_values(f"A1:ZZ{HEADER_SEARCH_ROWS}")
    next_row = _find_next_empty_row(ws)

    field_to_col = {
        "company": _find_column(header_values, TARGET_COMPANY_HEADER_CANDIDATES),
        "url": _find_column(header_values, TARGET_URL_HEADER_CANDIDATES),
        "contact": _find_column(header_values, TARGET_CONTACT_HEADER_CANDIDATES),
        "lastname": _find_column(header_values, TARGET_LASTNAME_HEADER_CANDIDATES),
        "firstname": _find_column(header_values, TARGET_FIRSTNAME_HEADER_CANDIDATES, exact=True),
        "email": _find_column(header_values, TARGET_EMAIL_HEADER_CANDIDATES),
    }
    for field, col in field_to_col.items():
        value = ref_data.get(field, "")
        if col is not None and value:
            write_with_retry(ws, next_row, col, value)

    # 送付日は実際に資料を送った時点で手動記入する運用のため、ここでは自動入力しない
    acquire_date_col = _find_column(header_values, TARGET_ACQUIRE_DATE_HEADER_CANDIDATES)
    if acquire_date_col is not None:
        write_with_retry(ws, next_row, acquire_date_col, date.today().strftime("%Y/%m/%d"))

    write_with_retry(ws, next_row, phone_col, ref_data.get("phone") or "")

    return next_row


# ------------------------------------------------------------------
# ジョブ処理 (ワーカー本体)
# ------------------------------------------------------------------
def process_pending_jobs():
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, phone_number, file_path, spreadsheet_url, kakudo, staff_name, uploader, chakuden_saki, report_type
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
        _process_single_job(
            row.id, row.phone_number, row.file_path, row.spreadsheet_url,
            row.kakudo, row.staff_name, row.uploader, row.chakuden_saki, row.report_type,
        )


def _process_single_job(
    job_id: int,
    phone_number: str,
    file_path: str,
    spreadsheet_url: str,
    kakudo: str | None = None,
    staff_name: str | None = None,
    uploader: str | None = None,
    chakuden_saki: str | None = None,
    report_type: str = "資料送付",
):
    tab_keyword = REPORT_TYPE_TAB_KEYWORDS.get(report_type, TAB_NAME_KEYWORD)
    prompt = REPORT_TYPE_PROMPTS.get(report_type, PROMPT)

    summary_text = None
    gemini_error = None
    try:
        summary_text = transcribe_and_summarize(file_path, prompt)
    except Exception as e:
        # Geminiの文字起こし・要約が失敗しても、電話番号キーでの行特定・確度・
        # お名前などの他項目の書き込みは続行する(ヒアリング内容欄だけが空欄になる)。
        logger.exception("job=%d Gemini処理失敗(他の項目の書き込みは続行します)", job_id)
        gemini_error = str(e)
    finally:
        try:
            os.remove(file_path)
        except OSError:
            pass

    try:
        ws = resolve_worksheet(spreadsheet_url, tab_keyword)
        phone_col, detail_col, kakudo_col = find_header_columns(ws)
        row_num = find_row_by_phone(ws, phone_col, phone_number)

        if row_num is None:
            ref_ws = find_reference_worksheet(ws.spreadsheet)
            ref_data = lookup_reference_row(ref_ws, phone_number) if ref_ws else None
            if ref_data is None:
                _update_job(job_id, "no_match", result_text=summary_text, error_message=gemini_error)
                notify_slack_no_match(job_id, phone_number, summary_text)
                return
            row_num = append_row_from_reference(ws, phone_col, ref_data)
            logger.info(
                "job=%d 「%s」に見つからなかったため「%s」から情報を引いて%d行目に新規追加",
                job_id, ws.title, ref_ws.title, row_num,
            )

        if summary_text:
            write_with_retry(ws, row_num, detail_col, summary_text)
        if kakudo:
            if kakudo_col is not None:
                write_with_retry(ws, row_num, kakudo_col, kakudo)
            else:
                logger.warning("job=%d 確度列が見つからないため確度の書き込みをスキップしました", job_id)

        if uploader or chakuden_saki:
            header_values = ws.get_values(f"A1:ZZ{HEADER_SEARCH_ROWS}")

            if uploader:
                lastname_col = _find_column(header_values, TARGET_LASTNAME_HEADER_CANDIDATES)
                if lastname_col is not None:
                    write_with_retry(ws, row_num, lastname_col, uploader)
                else:
                    logger.warning("job=%d 姓列が見つからないためお名前の書き込みをスキップしました", job_id)

            if chakuden_saki:
                contact_col = _find_column(header_values, TARGET_CONTACT_HEADER_CANDIDATES)
                if contact_col is not None:
                    write_with_retry(ws, row_num, contact_col, chakuden_saki)
                else:
                    logger.warning("job=%d 着電先列が見つからないため書き込みをスキップしました", job_id)
    except Exception as e:
        logger.exception("job=%d シート書き込み失敗", job_id)
        _update_job(job_id, "error", error_message=str(e), result_text=summary_text)
        notify_slack_failure(job_id, phone_number)
        return

    if gemini_error:
        # 他項目は書き込み済みだが、ヒアリング内容(Geminiの要約)だけは手動記入が必要。
        # 行自体の特定・更新は完了しているので、Slack通知は通常どおり出す。
        _update_job(
            job_id, "error",
            error_message=f"Gemini処理失敗(電話番号キーでの他項目書き込みは完了): {gemini_error}",
            result_text=summary_text,
        )
    else:
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
        report_type=report_type,
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
    preview = summary_text[:80] if summary_text else "(Gemini処理失敗のため要約なし)"
    logger.info("[Slack成功通知] %d行目更新 job=%d\n%s", row_num, job_id, preview)


def notify_slack_no_match(job_id, phone_number, summary_text):
    logger.info("[Slack要確認通知] 電話番号%s の行が見つかりません job=%d", phone_number, job_id)


def notify_slack_failure(job_id, phone_number):
    logger.info("[Slack失敗通知] phone=%s job=%d", phone_number, job_id)


def trigger_shiryou_soufu_workflow(
    official_deal_name: str, spreadsheet_url: str, uploader_mention: str = "",
    report_type: str = "資料送付",
):
    """Slackワークフロー(資料送付報告_v2 / アポ獲得報告_v2_Webhook)のWebhookトリガーを起動する。

    report_typeに応じて宛先のWebhookを切り替える。
    """
    webhook_url = SLACK_WORKFLOW_WEBHOOK_URL_APO if report_type == "アポ獲得" else SLACK_WORKFLOW_WEBHOOK_URL
    if not webhook_url:
        logger.info("[Slackワークフロー] report_type=%s 用のWebhook URLが未設定のためスキップ", report_type)
        return
    try:
        response = requests.post(
            webhook_url,
            json={
                "official_deal_name": official_deal_name,
                "spreadsheet_url": spreadsheet_url,
                "uploader_mention": uploader_mention,
            },
            timeout=10,
        )
        response.raise_for_status()
        logger.info(
            "[Slackワークフロー] 起動成功 report_type=%s official_deal_name=%s status=%s",
            report_type, official_deal_name, response.status_code,
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


def sync_staff_members_from_slack():
    """SLACK_BOT_TOKENを使い、STAFF_SYNC_CHANNEL_IDのメンバー一覧でstaff_membersを更新する。

    毎日1回、実施者(架電担当者)一覧をSlackチャンネルのメンバーと同期する。
    Bot/削除済みユーザーは除外し、表示名(表示名が無ければ本名)をnameとして登録する。
    SLACK_BOT_TOKEN未設定時は何もしない(手動登録のみの運用と互換)。
    """
    if not SLACK_BOT_TOKEN:
        logger.info("[実施者同期] SLACK_BOT_TOKEN未設定のためスキップ")
        return

    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

    member_ids: list[str] = []
    cursor = None
    while True:
        params = {"channel": STAFF_SYNC_CHANNEL_ID, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(
            "https://slack.com/api/conversations.members",
            headers=headers, params=params, timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.error("[実施者同期] conversations.members失敗: %s", data.get("error"))
            return
        member_ids.extend(data.get("members", []))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    synced = 0
    with engine.begin() as conn:
        for user_id in member_ids:
            resp = requests.get(
                "https://slack.com/api/users.info",
                headers=headers, params={"user": user_id}, timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.warning("[実施者同期] users.info失敗 user=%s: %s", user_id, data.get("error"))
                continue

            user = data["user"]
            if user.get("is_bot") or user.get("deleted") or user_id == "USLACKBOT":
                continue

            profile = user.get("profile", {})
            name = profile.get("real_name") or user.get("real_name") or profile.get("display_name")
            if not name:
                continue

            conn.execute(
                text(
                    """
                    INSERT INTO staff_members (name, slack_user_id)
                    VALUES (:name, :slack_user_id)
                    ON CONFLICT (name) DO UPDATE SET slack_user_id = EXCLUDED.slack_user_id
                    """
                ),
                {"name": name, "slack_user_id": user_id},
            )
            synced += 1

    logger.info("[実施者同期] %d名を同期しました", synced)


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
    report_type: str = Form(...),
    kakudo: str = Form(""),
    chakuden_saki: str = Form(""),
    staff_name: str = Form(...),
    audio_file: UploadFile = File(...),
):
    if not spreadsheet_url.strip():
        raise HTTPException(status_code=400, detail="スプレッドシートURLを入力してください")

    if report_type not in REPORT_TYPE_CHOICES:
        raise HTTPException(status_code=400, detail=f"報告種別は{REPORT_TYPE_CHOICES}のいずれかを選択してください")

    if report_type == "資料送付":
        if kakudo not in KAKUDO_CHOICES:
            raise HTTPException(status_code=400, detail=f"確度は{KAKUDO_CHOICES}のいずれかを選択してください")
        if chakuden_saki not in CHAKUDEN_SAKI_CHOICES:
            raise HTTPException(status_code=400, detail=f"着電先は{CHAKUDEN_SAKI_CHOICES}のいずれかを選択してください")
    else:
        kakudo = ""
        chakuden_saki = ""

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
                INSERT INTO audio_jobs (phone_number, uploader, file_path, spreadsheet_url, kakudo, chakuden_saki, staff_name, report_type)
                VALUES (:phone_number, :uploader, :file_path, :spreadsheet_url, :kakudo, :chakuden_saki, :staff_name, :report_type)
                """
            ),
            {
                "phone_number": phone_number,
                "uploader": uploader,
                "file_path": str(saved_path),
                "spreadsheet_url": spreadsheet_url.strip(),
                "kakudo": kakudo,
                "chakuden_saki": chakuden_saki,
                "staff_name": staff_name,
                "report_type": report_type,
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
    scheduler.add_job(sync_staff_members_from_slack, "interval", hours=STAFF_SYNC_INTERVAL_HOURS)
    scheduler.start()
    logger.info("ワーカーを起動しました (interval=%ds, batch=%d)", WORKER_INTERVAL_SECONDS, MAX_JOBS_PER_TICK)

    # 起動直後は次回同期(24時間後)まで待たず、一度すぐに実施者一覧を最新化する
    try:
        sync_staff_members_from_slack()
    except Exception:
        logger.exception("[実施者同期] 起動時の同期に失敗しました")


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
