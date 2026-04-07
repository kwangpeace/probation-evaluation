# -*- coding: utf-8 -*-
import os
import json
import secrets
import sqlite3
from io import BytesIO
from datetime import datetime, date
from pathlib import Path

import requests
from flask import Flask, Response, abort, g, redirect, render_template_string, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
try:
    import boto3
except ImportError:
    boto3 = None
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build as gbuild
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
except ImportError:
    service_account = None
    gbuild = None
    MediaIoBaseDownload = None
    MediaIoBaseUpload = None
try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "probation_eval.db"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
_db_initialized = False
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
GOOGLE_DRIVE_ENABLED = os.getenv("GOOGLE_DRIVE_ENABLED", "false").lower() in ("1", "true", "yes")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
_drive_service = None
OBJECT_STORAGE_ENABLED = os.getenv("OBJECT_STORAGE_ENABLED", "false").lower() in ("1", "true", "yes")
OBJECT_STORAGE_ENDPOINT = os.getenv("OBJECT_STORAGE_ENDPOINT", "").strip()
OBJECT_STORAGE_REGION = os.getenv("OBJECT_STORAGE_REGION", "auto").strip()
OBJECT_STORAGE_ACCESS_KEY = os.getenv("OBJECT_STORAGE_ACCESS_KEY", "").strip()
OBJECT_STORAGE_SECRET_KEY = os.getenv("OBJECT_STORAGE_SECRET_KEY", "").strip()
OBJECT_STORAGE_BUCKET = os.getenv("OBJECT_STORAGE_BUCKET", "").strip()
_object_storage_client = None

GRADE_TO_SCORE = {"S": 4, "A": 3, "B": 2, "C": 1}
SCORE_TO_GRADE = {4: "S", 3: "A", 2: "B", 1: "C"}
PEER_VISIBILITY_OPTIONS = {
    "evaluator_only": "평가자만 공개",
    "admin_only": "관리자만 공개",
    "admin_and_evaluator": "관리자+평가자 공개",
}
RELATIONSHIP_OPTIONS = {
    "direct_leader": "직속 리더",
    "other_leader": "타팀 리더",
    "mentor": "멘토",
    "hr": "HR 담당자",
    "other": "기타",
}
DECISION_LABELS = {
    "SUPER_PASS": "합격 (핵심인재)",
    "PASS": "합격 (우수인재)",
    "EXTENSION": "수습 연장",
    "FAIL": "불합격",
    "IN_PROGRESS": "진행 중",
}


def now():
    return datetime.now().isoformat(timespec="seconds")


def today_str():
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class DBWrapper:
    def __init__(self, conn, is_postgres=False):
        self.conn = conn
        self.is_postgres = is_postgres

    def _sql(self, query):
        if not self.is_postgres:
            return query
        return query.replace("?", "%s")

    def execute(self, query, params=()):
        return self.conn.execute(self._sql(query), params)

    def executemany(self, query, seq_of_params):
        return self.conn.executemany(self._sql(query), seq_of_params)

    def executescript(self, script):
        if not self.is_postgres:
            self.conn.executescript(script)
            return
        statements = [s.strip() for s in script.split(";") if s.strip()]
        for stmt in statements:
            self.conn.execute(stmt)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def connect_db():
    if USE_POSTGRES:
        if psycopg is None:
            raise RuntimeError("psycopg is required when DATABASE_URL is set")
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        return DBWrapper(conn, is_postgres=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return DBWrapper(conn, is_postgres=False)


def get_db():
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def scalar_count(db, table_name):
    row = db.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
    if row is None:
        return 0
    if isinstance(row, dict):
        return int(row.get("count", 0))
    if hasattr(row, "keys") and "count" in row.keys():
        return int(row["count"])
    return int(row[0])


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def get_drive_service():
    global _drive_service
    if not GOOGLE_DRIVE_ENABLED:
        return None
    if service_account is None or gbuild is None:
        return None
    if _drive_service is not None:
        return _drive_service
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    cred_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    scopes = ["https://www.googleapis.com/auth/drive"]
    if raw_json:
        info = json.loads(raw_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    elif cred_file:
        creds = service_account.Credentials.from_service_account_file(cred_file, scopes=scopes)
    else:
        return None
    _drive_service = gbuild("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_service


def upload_to_google_drive(filename, file_bytes, content_type):
    service = get_drive_service()
    if service is None:
        return None
    media = MediaIoBaseUpload(BytesIO(file_bytes), mimetype=content_type or "application/octet-stream", resumable=False)
    body = {"name": filename}
    if GOOGLE_DRIVE_FOLDER_ID:
        body["parents"] = [GOOGLE_DRIVE_FOLDER_ID]
    created = service.files().create(body=body, media_body=media, fields="id").execute()
    return created.get("id")


def download_from_google_drive(file_id):
    service = get_drive_service()
    if service is None or not file_id:
        return None, None, None
    meta = service.files().get(fileId=file_id, fields="name,mimeType").execute()
    request_obj = service.files().get_media(fileId=file_id)
    buf = BytesIO()
    downloader = MediaIoBaseDownload(buf, request_obj)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return meta.get("name") or "presentation", meta.get("mimeType") or "application/octet-stream", buf.read()


def get_object_storage_client():
    global _object_storage_client
    if not OBJECT_STORAGE_ENABLED or boto3 is None:
        return None
    if _object_storage_client is not None:
        return _object_storage_client
    if not (OBJECT_STORAGE_ENDPOINT and OBJECT_STORAGE_ACCESS_KEY and OBJECT_STORAGE_SECRET_KEY and OBJECT_STORAGE_BUCKET):
        return None
    _object_storage_client = boto3.client(
        "s3",
        endpoint_url=OBJECT_STORAGE_ENDPOINT,
        aws_access_key_id=OBJECT_STORAGE_ACCESS_KEY,
        aws_secret_access_key=OBJECT_STORAGE_SECRET_KEY,
        region_name=OBJECT_STORAGE_REGION,
    )
    return _object_storage_client


def upload_to_object_storage(object_key, file_bytes, content_type):
    client = get_object_storage_client()
    if client is None:
        return False
    try:
        client.put_object(Bucket=OBJECT_STORAGE_BUCKET, Key=object_key, Body=file_bytes, ContentType=content_type or "application/octet-stream")
        return True
    except Exception as exc:
        app.logger.exception("Object storage upload failed: %s", exc)
        return False


def download_from_object_storage(object_key):
    client = get_object_storage_client()
    if client is None or not object_key:
        return None, None
    try:
        resp = client.get_object(Bucket=OBJECT_STORAGE_BUCKET, Key=object_key)
        return resp.get("ContentType") or "application/octet-stream", resp["Body"].read()
    except Exception as exc:
        app.logger.exception("Object storage download failed: %s", exc)
        return None, None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db():
    db = connect_db()
    if USE_POSTGRES:
        schema_script = """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL CHECK (role IN ('admin','target','evaluator')),
            password_hash TEXT, team TEXT, access_start TEXT, access_end TEXT
        );
        CREATE TABLE IF NOT EXISTS evaluation_cycles (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, start_date TEXT, end_date TEXT,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS evaluatees (
            id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL, cycle_id INTEGER NOT NULL,
            peer_survey_token TEXT NOT NULL UNIQUE, presentation_filename TEXT,
            presentation_file_id TEXT, presentation_storage TEXT NOT NULL DEFAULT 'local',
            self_submitted_at TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evaluator_assignments (
            id SERIAL PRIMARY KEY, evaluatee_id INTEGER NOT NULL, evaluator_user_id INTEGER NOT NULL,
            relationship TEXT NOT NULL DEFAULT 'direct_leader',
            UNIQUE(evaluatee_id, evaluator_user_id)
        );
        CREATE TABLE IF NOT EXISTS assessment_items (
            id SERIAL PRIMARY KEY, code TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            prompt TEXT NOT NULL, grade_s TEXT NOT NULL, grade_a TEXT NOT NULL,
            grade_b TEXT NOT NULL, grade_c TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS self_assessments (
            id SERIAL PRIMARY KEY, evaluatee_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
            grade TEXT NOT NULL, keep_text TEXT, problem_text TEXT, try_text TEXT,
            updated_at TEXT NOT NULL, UNIQUE(evaluatee_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS leader_assessments (
            id SERIAL PRIMARY KEY, evaluatee_id INTEGER NOT NULL, evaluator_user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL, grade TEXT NOT NULL, feedback_text TEXT,
            presentation_note TEXT, qa_note TEXT, updated_at TEXT NOT NULL,
            UNIQUE(evaluatee_id, evaluator_user_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS peer_surveys (
            id SERIAL PRIMARY KEY, evaluatee_id INTEGER NOT NULL, peer_name TEXT,
            peer_comment TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS peer_feedback_assignments (
            id SERIAL PRIMARY KEY, evaluatee_id INTEGER NOT NULL, peer_reviewer_id INTEGER NOT NULL,
            UNIQUE(evaluatee_id, peer_reviewer_id)
        );
        CREATE TABLE IF NOT EXISTS aggregated_results (
            id SERIAL PRIMARY KEY, evaluatee_id INTEGER NOT NULL UNIQUE,
            decision TEXT NOT NULL, summary TEXT NOT NULL, admin_feedback TEXT,
            ai_polished_feedback TEXT, delivered_at TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_question_logs (
            id SERIAL PRIMARY KEY, evaluatee_id INTEGER NOT NULL, evaluator_user_id INTEGER NOT NULL,
            source_note TEXT NOT NULL, suggested_questions TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS peer_reviewers (id SERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS audit_logs (
            id SERIAL PRIMARY KEY, evaluatee_id INTEGER, actor_user_id INTEGER,
            action TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL
        );
        """
    else:
        schema_script = """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL CHECK (role IN ('admin','target','evaluator')),
            password_hash TEXT, team TEXT, access_start TEXT, access_end TEXT
        );
        CREATE TABLE IF NOT EXISTS evaluation_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, start_date TEXT, end_date TEXT,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS evaluatees (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, cycle_id INTEGER NOT NULL,
            peer_survey_token TEXT NOT NULL UNIQUE, presentation_filename TEXT,
            presentation_file_id TEXT, presentation_storage TEXT NOT NULL DEFAULT 'local',
            self_submitted_at TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evaluator_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, evaluatee_id INTEGER NOT NULL, evaluator_user_id INTEGER NOT NULL,
            relationship TEXT NOT NULL DEFAULT 'direct_leader',
            UNIQUE(evaluatee_id, evaluator_user_id)
        );
        CREATE TABLE IF NOT EXISTS assessment_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            prompt TEXT NOT NULL, grade_s TEXT NOT NULL, grade_a TEXT NOT NULL,
            grade_b TEXT NOT NULL, grade_c TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS self_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, evaluatee_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
            grade TEXT NOT NULL, keep_text TEXT, problem_text TEXT, try_text TEXT,
            updated_at TEXT NOT NULL, UNIQUE(evaluatee_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS leader_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, evaluatee_id INTEGER NOT NULL, evaluator_user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL, grade TEXT NOT NULL, feedback_text TEXT,
            presentation_note TEXT, qa_note TEXT, updated_at TEXT NOT NULL,
            UNIQUE(evaluatee_id, evaluator_user_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS peer_surveys (
            id INTEGER PRIMARY KEY AUTOINCREMENT, evaluatee_id INTEGER NOT NULL, peer_name TEXT,
            peer_comment TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS peer_feedback_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, evaluatee_id INTEGER NOT NULL, peer_reviewer_id INTEGER NOT NULL,
            UNIQUE(evaluatee_id, peer_reviewer_id)
        );
        CREATE TABLE IF NOT EXISTS aggregated_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, evaluatee_id INTEGER NOT NULL UNIQUE,
            decision TEXT NOT NULL, summary TEXT NOT NULL, admin_feedback TEXT,
            ai_polished_feedback TEXT, delivered_at TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_question_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, evaluatee_id INTEGER NOT NULL, evaluator_user_id INTEGER NOT NULL,
            source_note TEXT NOT NULL, suggested_questions TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS peer_reviewers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, evaluatee_id INTEGER, actor_user_id INTEGER,
            action TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL
        );
        """
    db.executescript(schema_script)

    if USE_POSTGRES:
        for col, typ in [("password_hash", "TEXT"), ("team", "TEXT"), ("access_start", "TEXT"), ("access_end", "TEXT")]:
            db.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {typ}")
        db.execute("ALTER TABLE evaluatees ADD COLUMN IF NOT EXISTS presentation_file_id TEXT")
        db.execute("ALTER TABLE evaluatees ADD COLUMN IF NOT EXISTS presentation_storage TEXT NOT NULL DEFAULT 'local'")
        db.execute("ALTER TABLE evaluator_assignments ADD COLUMN IF NOT EXISTS relationship TEXT NOT NULL DEFAULT 'direct_leader'")
        db.execute("ALTER TABLE aggregated_results ADD COLUMN IF NOT EXISTS ai_polished_feedback TEXT")
    else:
        def _add_col_if_missing(table, col, typ):
            cols = db.execute(f"PRAGMA table_info({table})").fetchall()
            names = {c["name"] if hasattr(c, "keys") else c[1] for c in cols}
            if col not in names:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        for col, typ in [("password_hash", "TEXT"), ("team", "TEXT"), ("access_start", "TEXT"), ("access_end", "TEXT")]:
            _add_col_if_missing("users", col, typ)
        _add_col_if_missing("evaluatees", "presentation_file_id", "TEXT")
        _add_col_if_missing("evaluatees", "presentation_storage", "TEXT NOT NULL DEFAULT 'local'")
        _add_col_if_missing("evaluator_assignments", "relationship", "TEXT NOT NULL DEFAULT 'direct_leader'")
        _add_col_if_missing("aggregated_results", "ai_polished_feedback", "TEXT")

    if scalar_count(db, "users") == 0:
        db.executemany(
            "INSERT INTO users(name, email, role, password_hash, team) VALUES (?, ?, ?, ?, ?)",
            [
                ("HR 관리자", "admin@company.local", "admin", generate_password_hash("admin1234"), "인사팀"),
                ("대상자 김수습", "target1@company.local", "target", generate_password_hash("target1234"), "개발팀"),
                ("대상자 박수습", "target2@company.local", "target", generate_password_hash("target1234"), "디자인팀"),
                ("평가자 이리더", "leader1@company.local", "evaluator", generate_password_hash("leader1234"), "개발팀"),
                ("평가자 최리더", "leader2@company.local", "evaluator", generate_password_hash("leader1234"), "디자인팀"),
                ("평가자 정리더", "leader3@company.local", "evaluator", generate_password_hash("leader1234"), "기획팀"),
            ],
        )
    else:
        db.execute("UPDATE users SET password_hash=? WHERE password_hash IS NULL AND role='admin'", (generate_password_hash("admin1234"),))
        db.execute("UPDATE users SET password_hash=? WHERE password_hash IS NULL AND role='target'", (generate_password_hash("target1234"),))
        db.execute("UPDATE users SET password_hash=? WHERE password_hash IS NULL AND role='evaluator'", (generate_password_hash("leader1234"),))
    if scalar_count(db, "evaluation_cycles") == 0:
        db.execute("INSERT INTO evaluation_cycles(name, start_date, end_date) VALUES (?, ?, ?)", ("2026년 1분기 수습평가", "2026-01-01", "2026-03-31"))
    if scalar_count(db, "assessment_items") == 0:
        db.executemany(
            "INSERT INTO assessment_items(code,title,prompt,grade_s,grade_a,grade_b,grade_c) VALUES (?,?,?,?,?,?,?)",
            [
                ("TEAM_CONTRIBUTION", "팀목표 기여도", "입사 시 합의된 우리 팀의 당면 과제 해결에 본인의 업무가 실제로 기여했습니까?",
                 "핵심문제 해결 또는 역할 범위를 넘어 팀 목표 달성에 결정적 기여",
                 "합의된 역할 내 임무를 충실히 수행해 팀 과제 해결에 기여",
                 "업무 수행은 했으나 주도성이 부족해 지속 가이드 필요",
                 "팀 과제와 무관한 업무 또는 결과물 품질 미달로 팀에 부담"),
                ("TASK_ACHIEVEMENT", "핵심과제 달성도", "합의서에 명시된 3개월 내 기대성과를 정성/정량적으로 달성했습니까?",
                 "목표 120% 이상 또는 기대 수준을 훨씬 상회",
                 "목표 100% 달성 및 합의된 품질 충족",
                 "목표 약 80% 달성 또는 일정/품질 보완 필요",
                 "달성율 70% 미만 또는 실무 활용이 어려운 품질"),
                ("BEHAVIOR_ALIGNMENT", "기대행동 부합도", "합의서에 명시된 기대행동을 준수하였습니까?",
                 "완벽 준수를 넘어 타인 모범 또는 더 나은 행동 양식 제안",
                 "합의된 행동가이드를 예외 없이 준수",
                 "대체로 준수했으나 특정 상황에서 행동 교정 필요",
                 "행동기준 반복 위반 및 개선 요청에도 변화 부족"),
            ],
        )
    db.execute("INSERT INTO app_settings(key, value) VALUES ('peer_visibility', 'evaluator_only') ON CONFLICT(key) DO NOTHING")
    if scalar_count(db, "peer_reviewers") == 0:
        db.executemany("INSERT INTO peer_reviewers(name) VALUES (?)", [("동료피드백 참여자 김동료",), ("동료피드백 참여자 이동료",), ("동료피드백 참여자 박동료",)])
    db.commit()
    db.close()


def ensure_db_initialized():
    global _db_initialized
    if _db_initialized:
        return
    init_db()
    _db_initialized = True


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def row_value(row, key, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    return default


def is_within_access_period(user):
    if not user:
        return False
    if user["role"] == "admin":
        return True
    t = today_str()
    start = row_value(user, "access_start")
    end = row_value(user, "access_end")
    if start and t < start:
        return False
    if end and t > end:
        return False
    return True


def require_role(role=None):
    def deco(fn):
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if role and user["role"] != role:
                abort(403)
            if not is_within_access_period(user):
                return render_template_string(COMMON_STYLE + """
                <div style="text-align:center;margin-top:80px;">
                  <div style="font-size:48px;margin-bottom:16px;">🔒</div>
                  <h2 style="border:none;">접근 기간이 아닙니다</h2>
                  <p style="color:#666;">현재 계정의 접근 허용 기간이 아닙니다.<br/>관리자에게 문의해 주세요.</p>
                  <a href="/logout" class="btn" style="display:inline-block;margin-top:20px;text-decoration:none;">로그아웃</a>
                </div>""" + FOOTER)
            return fn(*args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


# ---------------------------------------------------------------------------
# Business logic helpers
# ---------------------------------------------------------------------------

def log_action(db, action, evaluatee_id=None, actor_user_id=None, detail=""):
    db.execute(
        "INSERT INTO audit_logs(evaluatee_id, actor_user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (evaluatee_id, actor_user_id, action, detail, now()),
    )


def get_evaluatee_progress(db, evaluatee_id):
    evaluatee = db.execute(
        "SELECT self_submitted_at FROM evaluatees WHERE id=?",
        (evaluatee_id,),
    ).fetchone()
    self_done = bool(evaluatee and evaluatee["self_submitted_at"])
    assigned_count_row = db.execute(
        "SELECT COUNT(*) AS count FROM evaluator_assignments WHERE evaluatee_id=?",
        (evaluatee_id,),
    ).fetchone()
    assigned_count = row_value(assigned_count_row, "count", 0) or 0
    evaluator_done = db.execute(
        """
        SELECT COUNT(DISTINCT evaluator_user_id) AS count
        FROM leader_assessments
        WHERE evaluatee_id=? AND feedback_text IS NOT NULL AND feedback_text != ''
        """,
        (evaluatee_id,),
    ).fetchone()
    evaluator_done_count = row_value(evaluator_done, "count", 0) or 0
    result = db.execute(
        "SELECT decision, delivered_at FROM aggregated_results WHERE evaluatee_id=?",
        (evaluatee_id,),
    ).fetchone()
    aggregated = bool(result)
    delivered = bool(result and result["delivered_at"])

    if delivered:
        status = "전달 완료"
    elif aggregated:
        status = "취합 완료"
    elif evaluator_done_count and assigned_count and evaluator_done_count >= assigned_count:
        status = "평가 완료"
    elif self_done:
        status = "리더 평가 중"
    else:
        status = "자가평가 대기"

    progress_score = 0
    if self_done:
        progress_score += 1
    if evaluator_done_count:
        progress_score += 1
    if aggregated:
        progress_score += 1
    if delivered:
        progress_score += 1
    return {
        "status": status,
        "self_done": self_done,
        "assigned_count": assigned_count,
        "evaluator_done_count": evaluator_done_count,
        "aggregated": aggregated,
        "delivered": delivered,
        "progress_score": progress_score,
    }


def get_audit_logs_for_evaluatee(db, evaluatee_id, limit=20):
    return db.execute(
        """
        SELECT al.*, u.name AS actor_name
        FROM audit_logs al
        LEFT JOIN users u ON u.id = al.actor_user_id
        WHERE al.evaluatee_id=?
        ORDER BY al.id DESC
        LIMIT ?
        """,
        (evaluatee_id, limit),
    ).fetchall()


def get_assigned_peer_reviewers(db, evaluatee_id):
    return db.execute(
        """
        SELECT pr.id, pr.name
        FROM peer_feedback_assignments pfa
        JOIN peer_reviewers pr ON pr.id = pfa.peer_reviewer_id
        WHERE pfa.evaluatee_id=?
        ORDER BY pr.name
        """,
        (evaluatee_id,),
    ).fetchall()


def summarize_peer_comments(db, evaluatee_id):
    rows = db.execute("SELECT peer_comment FROM peer_surveys WHERE evaluatee_id = ? ORDER BY id DESC", (evaluatee_id,)).fetchall()
    if not rows:
        return "수집된 동료 의견이 없습니다."
    comments = [r["peer_comment"] for r in rows]
    return "총 {}건 수집. 주요 의견: {}".format(len(comments), "; ".join(comments[:3]))


def get_peer_visibility(db):
    row = db.execute("SELECT value FROM app_settings WHERE key='peer_visibility'").fetchone()
    if not row:
        return "evaluator_only"
    value = row["value"]
    return value if value in PEER_VISIBILITY_OPTIONS else "evaluator_only"


def get_item_grades_for_evaluatee(db, evaluatee_id):
    rows = db.execute("SELECT item_id, grade FROM leader_assessments WHERE evaluatee_id = ?", (evaluatee_id,)).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["item_id"], []).append(row["grade"])
    result = {}
    for item_id, grades in grouped.items():
        avg = sum(GRADE_TO_SCORE[g] for g in grades) / len(grades)
        rounded = max(1, min(4, int(round(avg))))
        result[item_id] = SCORE_TO_GRADE[rounded]
    return result


def decide_result(item_grades):
    grades = list(item_grades.values())
    if not grades:
        return "IN_PROGRESS"
    s_count = grades.count("S")
    a_count = grades.count("A")
    c_count = grades.count("C")
    b_count = grades.count("B")
    if c_count >= 1:
        return "FAIL"
    if (a_count + s_count) == len(grades) and s_count >= 2:
        return "SUPER_PASS"
    if (a_count + s_count) >= 2:
        return "PASS"
    if b_count >= 2:
        return "EXTENSION"
    return "PASS"


def build_ai_questions(note):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return ("1. 이번 발표의 핵심 성과를 수치 중심으로 다시 설명해 주세요.\n"
                "2. 가장 어려웠던 의사결정 순간과 판단 기준은 무엇이었나요?\n"
                "3. 협업 과정의 병목을 어떻게 해결했나요?\n"
                "4. 같은 과제를 다시 수행한다면 어떤 점을 바꾸겠나요?\n"
                "5. 다음 90일 동안의 최우선 개선 항목 1가지는 무엇인가요?")
    prompt = ("Generate 5 probation-review questions in Korean from this note. "
              "Focus on concrete behavior, evidence, and improvement actions.\n\nNote:\n" + note)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return "AI 질문 생성에 실패했습니다. 잠시 후 다시 시도해주세요."


def polish_feedback_with_ai(feedbacks_text, target_name):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return feedbacks_text
    prompt = (
        f"다음은 수습 평가 대상자 '{target_name}'에 대한 여러 평가자의 피드백입니다.\n"
        "이 피드백들을 하나의 종합 피드백으로 정리해주세요.\n"
        "요구사항:\n"
        "1. 맞춤법과 문법을 교정해주세요\n"
        "2. 중복되는 내용을 통합하세요\n"
        "3. 긍정적 피드백과 개선 필요 사항을 구분해서 정리하세요\n"
        "4. 존댓말로 작성하세요\n"
        "5. 구체적이고 건설적인 톤을 유지하세요\n\n"
        f"평가자 피드백:\n{feedbacks_text}"
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        response = requests.post(url, json=payload, timeout=20)
        response.raise_for_status()
        data = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return feedbacks_text


# ---------------------------------------------------------------------------
# Design system
# ---------------------------------------------------------------------------

COMMON_STYLE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>수습평가 시스템</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --primary: #4F46E5; --primary-dark: #3730A3; --primary-light: #EEF2FF;
    --success: #059669; --success-bg: #ECFDF5;
    --warning: #D97706; --warning-bg: #FFFBEB;
    --danger: #DC2626; --danger-bg: #FEF2F2;
    --gray-50: #F9FAFB; --gray-100: #F3F4F6; --gray-200: #E5E7EB;
    --gray-300: #D1D5DB; --gray-400: #9CA3AF; --gray-500: #6B7280;
    --gray-600: #4B5563; --gray-700: #374151; --gray-800: #1F2937;
    --gray-900: #111827;
    --radius: 10px; --shadow: 0 1px 3px rgba(0,0,0,0.1), 0 1px 2px rgba(0,0,0,0.06);
    --shadow-md: 0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -1px rgba(0,0,0,0.06);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Inter', 'Malgun Gothic', -apple-system, sans-serif; background: var(--gray-50); color: var(--gray-800); line-height: 1.6; }
  .container { max-width: 1100px; margin: 0 auto; padding: 24px; }
  nav { background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%); padding: 14px 24px; margin-bottom: 28px; border-radius: var(--radius); display: flex; align-items: center; justify-content: space-between; box-shadow: var(--shadow-md); }
  nav .nav-links a { color: rgba(255,255,255,0.85); text-decoration: none; margin-right: 20px; font-size: 14px; font-weight: 500; transition: color 0.2s; }
  nav .nav-links a:hover { color: #fff; }
  nav .nav-right a { color: rgba(255,255,255,0.7); text-decoration: none; font-size: 13px; }
  nav .nav-right a:hover { color: #fff; }
  h1 { font-size: 28px; font-weight: 700; color: var(--gray-900); margin-bottom: 8px; }
  h2 { font-size: 22px; font-weight: 700; color: var(--gray-900); margin-bottom: 6px; border: none; padding: 0; }
  h3 { font-size: 17px; font-weight: 600; color: var(--gray-700); margin: 24px 0 12px; }
  .subtitle { color: var(--gray-500); font-size: 14px; margin-bottom: 24px; }
  .section { background: #fff; border-radius: var(--radius); padding: 24px; margin-bottom: 20px; box-shadow: var(--shadow); border: 1px solid var(--gray-200); }
  .card { background: #fff; border: 1px solid var(--gray-200); padding: 18px; margin: 12px 0; border-radius: var(--radius); transition: box-shadow 0.2s; }
  .card:hover { box-shadow: var(--shadow-md); }
  table { border-collapse: collapse; width: 100%; font-size: 14px; }
  th, td { padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--gray-200); }
  th { background: var(--gray-50); color: var(--gray-600); font-weight: 600; font-size: 13px; text-transform: uppercase; letter-spacing: 0.03em; }
  tr:hover { background: var(--gray-50); }
  .btn { display: inline-block; padding: 10px 22px; font-size: 14px; font-weight: 600; border: none; border-radius: 8px; cursor: pointer; transition: all 0.2s; text-decoration: none; }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover { background: var(--primary-dark); transform: translateY(-1px); box-shadow: var(--shadow-md); }
  .btn-success { background: var(--success); color: #fff; }
  .btn-success:hover { background: #047857; }
  .btn-danger { background: var(--danger); color: #fff; font-size: 12px; padding: 6px 14px; }
  .btn-danger:hover { background: #B91C1C; }
  .btn-sm { padding: 6px 14px; font-size: 12px; }
  .btn-outline { background: transparent; border: 1px solid var(--gray-300); color: var(--gray-600); }
  .btn-outline:hover { border-color: var(--primary); color: var(--primary); }
  input[type="text"], input[type="email"], input[type="password"], input[type="date"], select, textarea {
    width: 100%; padding: 10px 14px; border: 1px solid var(--gray-300); border-radius: 8px; font-size: 14px;
    font-family: inherit; transition: border-color 0.2s, box-shadow 0.2s; background: #fff;
  }
  input:focus, select:focus, textarea:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(79,70,229,0.1); }
  label { display: block; font-size: 13px; font-weight: 600; color: var(--gray-700); margin-bottom: 6px; }
  .form-group { margin-bottom: 16px; }
  .form-row { display: flex; gap: 16px; flex-wrap: wrap; }
  .form-row > .form-group { flex: 1; min-width: 200px; }
  fieldset { border: 1px solid var(--gray-200); border-radius: var(--radius); padding: 16px; margin: 12px 0; }
  legend { font-weight: 600; font-size: 14px; color: var(--gray-700); padding: 0 8px; }
  .badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }
  .badge-success { background: var(--success-bg); color: var(--success); }
  .badge-warning { background: var(--warning-bg); color: var(--warning); }
  .badge-danger { background: var(--danger-bg); color: var(--danger); }
  .badge-info { background: var(--primary-light); color: var(--primary); }
  .badge-gray { background: var(--gray-100); color: var(--gray-500); }
  .grade-selector { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0; }
  .grade-option { position: relative; }
  .grade-option input { position: absolute; opacity: 0; }
  .grade-option label { display: block; padding: 8px 16px; border: 2px solid var(--gray-200); border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 14px; text-align: center; transition: all 0.2s; min-width: 60px; }
  .grade-option input:checked + label { border-color: var(--primary); background: var(--primary-light); color: var(--primary); }
  .grade-option label:hover { border-color: var(--gray-400); }
  .grade-desc { font-size: 11px; color: var(--gray-500); display: block; margin-top: 2px; font-weight: 400; }
  .alert { padding: 14px 18px; border-radius: var(--radius); margin-bottom: 16px; font-size: 14px; }
  .alert-success { background: var(--success-bg); color: var(--success); border: 1px solid #A7F3D0; }
  .alert-warning { background: var(--warning-bg); color: var(--warning); border: 1px solid #FDE68A; }
  .alert-danger { background: var(--danger-bg); color: var(--danger); border: 1px solid #FECACA; }
  .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: #fff; border: 1px solid var(--gray-200); border-radius: var(--radius); padding: 20px; text-align: center; }
  .stat-card .stat-value { font-size: 32px; font-weight: 700; color: var(--primary); }
  .stat-card .stat-label { font-size: 13px; color: var(--gray-500); margin-top: 4px; }
  a { color: var(--primary); text-decoration: none; }
  a:hover { text-decoration: underline; }
  input[type="file"] { padding: 8px; }
  .link-copy { font-size: 12px; word-break: break-all; color: var(--gray-500); }
  @media print {
    nav, .btn, .no-print, form { display: none !important; }
    body { background: #fff; }
    .section { box-shadow: none; border: 1px solid #ddd; page-break-inside: avoid; }
    .container { max-width: 100%; padding: 0; }
  }
  @media (max-width: 768px) {
    .container { padding: 12px; }
    .form-row { flex-direction: column; }
    .stat-grid { grid-template-columns: 1fr 1fr; }
    table { font-size: 12px; }
    th, td { padding: 8px 6px; }
  }
</style>
</head>
<body>
<div class="container">
"""

FOOTER = "\n</div></body></html>"

NAV_ADMIN = """<nav>
<div class="nav-links">
  <a href="/admin">대시보드</a>
  <a href="/admin/users">사용자</a>
  <a href="/admin/cycles">사이클</a>
  <a href="/admin/peers">동료피드백 참여자</a>
</div>
<div class="nav-right"><a href="/logout">로그아웃</a></div>
</nav>"""

NAV_TARGET = """<nav>
<div class="nav-links"><a href="/target">대시보드</a></div>
<div class="nav-right"><a href="/logout">로그아웃</a></div>
</nav>"""

NAV_EVALUATOR = """<nav>
<div class="nav-links"><a href="/evaluator">대시보드</a></div>
<div class="nav-right"><a href="/logout">로그아웃</a></div>
</nav>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("dashboard" if current_user() else "login"))


@app.before_request
def bootstrap():
    ensure_db_initialized()


@app.route("/login", methods=["GET", "POST"])
def login():
    db = get_db()
    error = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and user["password_hash"] and check_password_hash(user["password_hash"], password):
            session["user_id"] = int(user["id"])
            return redirect(url_for("dashboard"))
        error = "이메일 또는 비밀번호가 올바르지 않습니다."
    return render_template_string(COMMON_STYLE + """
    <div style="max-width:420px;margin:60px auto;">
      <div class="section" style="text-align:center;">
        <h1 style="margin-bottom:4px;">수습평가 시스템</h1>
        <p class="subtitle">Probation Evaluation System</p>
        {% if error %}<div class="alert alert-danger">{{error}}</div>{% endif %}
        <form method="post" style="text-align:left;">
          <div class="form-group">
            <label>이메일</label>
            <input type="email" name="email" placeholder="example@company.local" required/>
          </div>
          <div class="form-group">
            <label>비밀번호</label>
            <input type="password" name="password" required/>
          </div>
          <button type="submit" class="btn btn-primary" style="width:100%;margin-top:8px;">로그인</button>
        </form>
      </div>
    </div>""" + FOOTER, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    if user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    if user["role"] == "target":
        return redirect(url_for("target_dashboard"))
    return redirect(url_for("evaluator_dashboard"))


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET", "POST"])
@require_role("admin")
def admin_dashboard():
    db = get_db()
    if request.method == "POST":
        if request.form.get("form_type") == "policy":
            pv = request.form.get("peer_visibility", "evaluator_only")
            if pv not in PEER_VISIBILITY_OPTIONS:
                pv = "evaluator_only"
            db.execute("INSERT INTO app_settings(key, value) VALUES ('peer_visibility', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (pv,))
            db.commit()
            return redirect(url_for("admin_dashboard"))

        target_user_id = request.form.get("target_user_id")
        cycle_id = request.form.get("cycle_id")
        evaluator_ids = request.form.getlist("evaluator_ids")[:3]
        peer_reviewer_ids = request.form.getlist("peer_reviewer_ids")
        cursor = db.execute("INSERT INTO evaluatees(user_id,cycle_id,peer_survey_token,created_at) VALUES (?,?,?,?) RETURNING id",
                            (target_user_id, cycle_id, secrets.token_hex(8), now()))
        inserted = cursor.fetchone()
        evaluatee_id = inserted["id"] if hasattr(inserted, "keys") else inserted[0]
        for evaluator_id in evaluator_ids:
            rel = request.form.get(f"relationship_{evaluator_id}", "direct_leader")
            if rel not in RELATIONSHIP_OPTIONS:
                rel = "direct_leader"
            db.execute("INSERT INTO evaluator_assignments(evaluatee_id,evaluator_user_id,relationship) VALUES (?,?,?) ON CONFLICT(evaluatee_id,evaluator_user_id) DO UPDATE SET relationship=excluded.relationship",
                       (evaluatee_id, evaluator_id, rel))
        for peer_reviewer_id in peer_reviewer_ids:
            db.execute(
                "INSERT INTO peer_feedback_assignments(evaluatee_id,peer_reviewer_id) VALUES (?,?) ON CONFLICT(evaluatee_id,peer_reviewer_id) DO NOTHING",
                (evaluatee_id, peer_reviewer_id),
            )
        log_action(db, "assignment_created", evaluatee_id=evaluatee_id, actor_user_id=current_user()["id"], detail=f"평가자 {len(evaluator_ids)}명 배정")
        db.commit()
        return redirect(url_for("admin_dashboard"))

    targets = db.execute("SELECT * FROM users WHERE role='target' ORDER BY id").fetchall()
    evaluators = db.execute("SELECT * FROM users WHERE role='evaluator' ORDER BY id").fetchall()
    peer_reviewers = db.execute("SELECT * FROM peer_reviewers ORDER BY name").fetchall()
    cycles = db.execute("SELECT * FROM evaluation_cycles ORDER BY id DESC").fetchall()
    peer_visibility = get_peer_visibility(db)
    evaluatees = db.execute("""
        SELECT e.id, u.name AS target_name, u.team AS target_team, c.name AS cycle_name,
               e.peer_survey_token, e.presentation_filename, ar.decision, e.self_submitted_at,
               COUNT(DISTINCT pfa.peer_reviewer_id) AS peer_feedback_count
        FROM evaluatees e JOIN users u ON u.id = e.user_id
        JOIN evaluation_cycles c ON c.id = e.cycle_id
        LEFT JOIN aggregated_results ar ON ar.evaluatee_id = e.id
        LEFT JOIN peer_feedback_assignments pfa ON pfa.evaluatee_id = e.id
        GROUP BY e.id, u.name, u.team, c.name, e.peer_survey_token, e.presentation_filename, ar.decision, e.self_submitted_at
        ORDER BY e.id DESC
    """).fetchall()

    total = len(evaluatees)
    submitted = sum(1 for e in evaluatees if e["self_submitted_at"])
    decided = sum(1 for e in evaluatees if e["decision"] and e["decision"] != "IN_PROGRESS")
    progress_map = {e["id"]: get_evaluatee_progress(db, e["id"]) for e in evaluatees}

    return render_template_string(COMMON_STYLE + NAV_ADMIN + """
    <h1>관리자 대시보드</h1>
    <p class="subtitle">수습평가 현황을 한눈에 확인하고 관리합니다</p>

    <div class="stat-grid">
      <div class="stat-card"><div class="stat-value">{{total}}</div><div class="stat-label">전체 대상자</div></div>
      <div class="stat-card"><div class="stat-value">{{submitted}}</div><div class="stat-label">자가평가 완료</div></div>
      <div class="stat-card"><div class="stat-value">{{decided}}</div><div class="stat-label">판정 완료</div></div>
      <div class="stat-card"><div class="stat-value">{{total - decided}}</div><div class="stat-label">진행 중</div></div>
    </div>

    <div class="section">
      <h3 style="margin-top:0;">동료피드백 공개 정책</h3>
      <form method="post" class="form-row" style="align-items:end;">
        <input type="hidden" name="form_type" value="policy"/>
        <div class="form-group" style="flex:2;">
          <select name="peer_visibility">
            {% for key, label in pv_options.items() %}
            <option value="{{key}}" {% if key == peer_visibility %}selected{% endif %}>{{label}}</option>
            {% endfor %}
          </select>
        </div>
        <div class="form-group" style="flex:0;"><button type="submit" class="btn btn-primary btn-sm">저장</button></div>
      </form>
    </div>

    <div class="section">
      <h3 style="margin-top:0;">평가 대상자 생성</h3>
      <form method="post">
        <div class="form-row">
          <div class="form-group">
            <label>대상자</label>
            <select name="target_user_id">{% for t in targets %}<option value="{{t.id}}">{{t.name}} ({{t.team or '-'}})</option>{% endfor %}</select>
          </div>
          <div class="form-group">
            <label>평가 사이클</label>
            <select name="cycle_id">{% for c in cycles %}<option value="{{c.id}}">{{c.name}}</option>{% endfor %}</select>
          </div>
        </div>
        <fieldset>
          <legend>평가자 배정 (최대 3명)</legend>
          {% for e in evaluators %}
          <div style="display:flex;align-items:center;gap:12px;margin:8px 0;">
            <label style="margin:0;display:flex;align-items:center;gap:6px;min-width:180px;">
              <input type="checkbox" name="evaluator_ids" value="{{e.id}}"> {{e.name}} <span style="color:var(--gray-400);font-weight:400;">({{e.team or '-'}})</span>
            </label>
            <select name="relationship_{{e.id}}" style="width:auto;padding:6px 10px;">
              {% for rk, rl in rel_options.items() %}<option value="{{rk}}">{{rl}}</option>{% endfor %}
            </select>
          </div>
          {% endfor %}
        </fieldset>
        <fieldset>
          <legend>동료피드백 참여자 배정</legend>
          <div class="form-row">
            {% for p in peer_reviewers %}
            <label style="display:flex;align-items:center;gap:6px;font-weight:500;">
              <input type="checkbox" name="peer_reviewer_ids" value="{{p.id}}"> {{p.name}}
            </label>
            {% endfor %}
          </div>
        </fieldset>
        <button type="submit" class="btn btn-primary">생성</button>
      </form>
    </div>

    <div class="section">
      <h3 style="margin-top:0;">대상자 목록</h3>
      <table>
        <tr><th>대상자</th><th>팀</th><th>사이클</th><th>진행 상태</th><th>자가평가</th><th>판정</th><th>동료피드백</th><th>액션</th></tr>
        {% for e in evaluatees %}
        <tr>
          <td><b>{{e.target_name}}</b></td>
          <td>{{e.target_team or '-'}}</td>
          <td>{{e.cycle_name}}</td>
          <td>
            {% set p = progress_map[e.id] %}
            {% if p.delivered %}<span class="badge badge-success">{{p.status}}</span>
            {% elif p.aggregated %}<span class="badge badge-info">{{p.status}}</span>
            {% elif p.evaluator_done_count %}<span class="badge badge-warning">{{p.status}}</span>
            {% else %}<span class="badge badge-gray">{{p.status}}</span>{% endif %}
            <div style="font-size:11px;color:var(--gray-400);margin-top:4px;">평가자 {{p.evaluator_done_count}} / {{p.assigned_count}}</div>
          </td>
          <td>{% if e.self_submitted_at %}<span class="badge badge-success">완료</span>{% else %}<span class="badge badge-gray">미제출</span>{% endif %}</td>
          <td>{% set d = e.decision or 'IN_PROGRESS' %}
              {% if d == 'SUPER_PASS' %}<span class="badge badge-success">{{dl[d]}}</span>
              {% elif d == 'PASS' %}<span class="badge badge-success">{{dl[d]}}</span>
              {% elif d == 'EXTENSION' %}<span class="badge badge-warning">{{dl[d]}}</span>
              {% elif d == 'FAIL' %}<span class="badge badge-danger">{{dl[d]}}</span>
              {% else %}<span class="badge badge-gray">{{dl[d]}}</span>{% endif %}</td>
          <td>
            <div><a href="{{url_for('peer_feedback', token=e.peer_survey_token, _external=True)}}" target="_blank" class="link-copy">링크 열기</a></div>
            <div style="font-size:11px;color:var(--gray-400);margin-top:4px;">배정 {{e.peer_feedback_count}}명</div>
          </td>
          <td>
            <a href="{{url_for('aggregate_result', evaluatee_id=e.id)}}" class="btn btn-outline btn-sm">취합</a>
            <a href="{{url_for('deliver_feedback', evaluatee_id=e.id)}}" class="btn btn-outline btn-sm">전달</a>
            <a href="{{url_for('admin_report', evaluatee_id=e.id)}}" class="btn btn-outline btn-sm">리포트</a>
            <a href="{{url_for('manage_peer_feedback_assignments', evaluatee_id=e.id)}}" class="btn btn-outline btn-sm">동료피드백 배정</a>
          </td>
        </tr>
        {% endfor %}
      </table>
    </div>
    """ + FOOTER, targets=targets, evaluators=evaluators, cycles=cycles,
        evaluatees=evaluatees, peer_visibility=peer_visibility,
        pv_options=PEER_VISIBILITY_OPTIONS, dl=DECISION_LABELS,
        rel_options=RELATIONSHIP_OPTIONS, total=total, submitted=submitted, decided=decided,
        progress_map=progress_map, peer_reviewers=peer_reviewers)


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------

@app.route("/target", methods=["GET", "POST"])
@require_role("target")
def target_dashboard():
    db = get_db()
    user = current_user()
    notice = request.args.get("notice", "")
    evaluatee = db.execute("SELECT * FROM evaluatees WHERE user_id=? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
    if not evaluatee:
        return render_template_string(COMMON_STYLE + NAV_TARGET + """
        <div class="section" style="text-align:center;padding:60px;">
          <h2>배정된 평가가 없습니다</h2>
          <p class="subtitle">관리자에게 문의해주세요.</p>
        </div>""" + FOOTER)
    items = db.execute("SELECT * FROM assessment_items ORDER BY id").fetchall()
    existing = db.execute("SELECT * FROM self_assessments WHERE evaluatee_id=?", (evaluatee["id"],)).fetchall()
    existing_by_item = {row["item_id"]: row for row in existing}
    if request.method == "POST":
        file = request.files.get("presentation")
        if file and file.filename:
            UPLOAD_DIR.mkdir(exist_ok=True)
            safe_name = secure_filename(file.filename) or "presentation.pdf"
            filename = f"{evaluatee['id']}_{int(datetime.now().timestamp())}_{safe_name}"
            try:
                content = file.read()
                if OBJECT_STORAGE_ENABLED:
                    object_key = f"presentations/{filename}"
                    if upload_to_object_storage(object_key, content, file.mimetype):
                        db.execute("UPDATE evaluatees SET presentation_filename=?, presentation_file_id=?, presentation_storage='s3' WHERE id=?", (filename, object_key, evaluatee["id"]))
                        notice = "PT 파일이 클라우드에 저장되었습니다."
                    else:
                        with open(UPLOAD_DIR / filename, "wb") as fp:
                            fp.write(content)
                        db.execute("UPDATE evaluatees SET presentation_filename=?, presentation_file_id=NULL, presentation_storage='local' WHERE id=?", (filename, evaluatee["id"]))
                        notice = "클라우드 연동 실패로 로컬에 저장되었습니다."
                else:
                    with open(UPLOAD_DIR / filename, "wb") as fp:
                        fp.write(content)
                    db.execute("UPDATE evaluatees SET presentation_filename=?, presentation_file_id=NULL, presentation_storage='local' WHERE id=?", (filename, evaluatee["id"]))
                    notice = "PT 파일이 저장되었습니다."
            except Exception:
                notice = "PT 파일 저장에 실패했습니다."
        keep_text = request.form.get("keep_text", "")
        problem_text = request.form.get("problem_text", "")
        try_text = request.form.get("try_text", "")
        for item in items:
            grade = request.form.get(f"grade_{item['id']}", "B")
            db.execute("""INSERT INTO self_assessments(evaluatee_id,item_id,grade,keep_text,problem_text,try_text,updated_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(evaluatee_id,item_id) DO UPDATE SET
                grade=excluded.grade, keep_text=excluded.keep_text, problem_text=excluded.problem_text,
                try_text=excluded.try_text, updated_at=excluded.updated_at""",
                       (evaluatee["id"], item["id"], grade, keep_text, problem_text, try_text, now()))
        db.execute("UPDATE evaluatees SET self_submitted_at=? WHERE id=?", (now(), evaluatee["id"]))
        log_action(db, "self_assessment_saved", evaluatee_id=evaluatee["id"], actor_user_id=user["id"], detail="자가평가 및 발표자료 저장")
        db.commit()
        return redirect(url_for("target_dashboard", notice=notice))
    result = db.execute("SELECT * FROM aggregated_results WHERE evaluatee_id=?", (evaluatee["id"],)).fetchone()
    progress = get_evaluatee_progress(db, evaluatee["id"])
    return render_template_string(COMMON_STYLE + NAV_TARGET + """
    <h1>평가 대상자 화면</h1>
    <p class="subtitle">{{user.name}} · {{user.team or ''}} · {{user.email}}</p>
    {% if notice %}<div class="alert alert-success">{{notice}}</div>{% endif %}

    <div class="section">
      <h3 style="margin-top:0;">내 진행 상태</h3>
      <div class="stat-grid">
        <div class="stat-card"><div class="stat-value">{{progress.progress_score}}/4</div><div class="stat-label">전체 진행도</div></div>
        <div class="stat-card"><div class="stat-value">{{'완료' if progress.self_done else '대기'}}</div><div class="stat-label">자가평가</div></div>
        <div class="stat-card"><div class="stat-value">{{progress.evaluator_done_count}}/{{progress.assigned_count}}</div><div class="stat-label">리더 평가</div></div>
        <div class="stat-card"><div class="stat-value">{{progress.status}}</div><div class="stat-label">현재 단계</div></div>
      </div>
    </div>

    <div class="section">
      <h3 style="margin-top:0;">PT 업로드 + 자가평가</h3>
      <form method="post" enctype="multipart/form-data">
        <div class="form-group">
          <label>PT 발표 자료</label>
          <input type="file" name="presentation"/>
          <p style="font-size:12px;color:var(--gray-500);margin-top:4px;">현재 파일: {% if evaluatee.presentation_filename %}<a href="{{url_for('presentation_file', evaluatee_id=evaluatee.id)}}">{{evaluatee.presentation_filename}}</a>{% else %}없음{% endif %}</p>
        </div>
        {% for item in items %}
        <div class="card">
          <b>{{item.title}}</b>
          <p style="font-size:13px;color:var(--gray-500);margin:4px 0 10px;">{{item.prompt}}</p>
          {% set current = existing_by_item[item.id].grade if item.id in existing_by_item else 'B' %}
          <div class="grade-selector">
            {% for g in ['S','A','B','C'] %}
            <div class="grade-option">
              <input type="radio" name="grade_{{item.id}}" value="{{g}}" id="g_{{item.id}}_{{g}}" {% if current==g %}checked{% endif %}>
              <label for="g_{{item.id}}_{{g}}">{{g}}
                <span class="grade-desc">{% if g=='S' %}{{item.grade_s}}{% elif g=='A' %}{{item.grade_a}}{% elif g=='B' %}{{item.grade_b}}{% else %}{{item.grade_c}}{% endif %}</span>
              </label>
            </div>
            {% endfor %}
          </div>
        </div>
        {% endfor %}
        <div class="form-group"><label>Keep (잘한 점)</label><textarea name="keep_text" rows="3">{{existing[0].keep_text if existing else ''}}</textarea></div>
        <div class="form-group"><label>Problem (아쉬운 점)</label><textarea name="problem_text" rows="3">{{existing[0].problem_text if existing else ''}}</textarea></div>
        <div class="form-group"><label>Try (개선 시도)</label><textarea name="try_text" rows="3">{{existing[0].try_text if existing else ''}}</textarea></div>
        <button type="submit" class="btn btn-primary">저장</button>
      </form>
    </div>

    <div class="section">
      <h3 style="margin-top:0;">평가 결과</h3>
      {% if result and result.delivered_at %}
        <div class="card">
          <p><b>판정:</b> {{dl.get(result.decision, result.decision)}}</p>
          <p style="margin-top:8px;"><b>피드백:</b></p>
          <div style="white-space:pre-wrap;background:var(--gray-50);padding:14px;border-radius:8px;margin-top:6px;">{{result.ai_polished_feedback or result.admin_feedback or '-'}}</div>
        </div>
      {% else %}
        <p style="color:var(--gray-500);">아직 결과가 전달되지 않았습니다.</p>
      {% endif %}
    </div>
    """ + FOOTER, items=items, existing=existing, existing_by_item=existing_by_item,
        result=result, evaluatee=evaluatee, user=user, dl=DECISION_LABELS, notice=notice,
        progress=progress)


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

@app.route("/evaluator", methods=["GET", "POST"])
@require_role("evaluator")
def evaluator_dashboard():
    db = get_db()
    user = current_user()
    assignments = db.execute("""
        SELECT e.id AS evaluatee_id, u.name AS target_name, u.team AS target_team,
               e.presentation_filename, ea.relationship
        FROM evaluator_assignments ea
        JOIN evaluatees e ON e.id = ea.evaluatee_id
        JOIN users u ON u.id = e.user_id
        WHERE ea.evaluator_user_id = ? ORDER BY e.id DESC
    """, (user["id"],)).fetchall()
    selected = request.args.get("evaluatee_id")
    if request.method == "POST":
        evaluatee_id = request.form.get("evaluatee_id")
        items = db.execute("SELECT * FROM assessment_items ORDER BY id").fetchall()
        for item in items:
            db.execute("""INSERT INTO leader_assessments(evaluatee_id,evaluator_user_id,item_id,grade,feedback_text,presentation_note,qa_note,updated_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(evaluatee_id,evaluator_user_id,item_id) DO UPDATE SET
                grade=excluded.grade, feedback_text=excluded.feedback_text,
                presentation_note=excluded.presentation_note, qa_note=excluded.qa_note, updated_at=excluded.updated_at""",
                       (evaluatee_id, user["id"], item["id"],
                        request.form.get(f"grade_{item['id']}", "B"),
                        request.form.get("feedback_text", ""),
                        request.form.get("presentation_note", ""),
                        request.form.get("qa_note", ""), now()))
        log_action(db, "leader_assessment_saved", evaluatee_id=evaluatee_id, actor_user_id=user["id"], detail="리더 평가 저장")
        db.commit()
        return redirect(url_for("evaluator_dashboard", evaluatee_id=evaluatee_id))
    detail = None; self_data = []; items = []; leader_data = {}; peer_summary = ""; ai_questions = ""
    if selected:
        assigned = db.execute("SELECT * FROM evaluator_assignments WHERE evaluatee_id=? AND evaluator_user_id=?", (selected, user["id"])).fetchone()
        if not assigned:
            abort(403)
        detail = db.execute("SELECT e.id, e.presentation_filename, u.name AS target_name, u.team AS target_team FROM evaluatees e JOIN users u ON u.id = e.user_id WHERE e.id = ?", (selected,)).fetchone()
        if not detail:
            abort(404)
        self_data = db.execute("""SELECT ai.title, sa.grade, sa.keep_text, sa.problem_text, sa.try_text
            FROM self_assessments sa JOIN assessment_items ai ON ai.id = sa.item_id
            WHERE sa.evaluatee_id = ? ORDER BY ai.id""", (selected,)).fetchall()
        items = db.execute("SELECT * FROM assessment_items ORDER BY id").fetchall()
        existing = db.execute("SELECT * FROM leader_assessments WHERE evaluatee_id=? AND evaluator_user_id=?", (selected, user["id"])).fetchall()
        leader_data = {row["item_id"]: row for row in existing}
        pv = get_peer_visibility(db)
        if pv in ("evaluator_only", "admin_and_evaluator"):
            peer_summary = summarize_peer_comments(db, selected)
        else:
            peer_summary = "관리자 전용 정책으로 비공개입니다."
        ai_row = db.execute("SELECT suggested_questions FROM ai_question_logs WHERE evaluatee_id=? AND evaluator_user_id=? ORDER BY id DESC LIMIT 1", (selected, user["id"])).fetchone()
        if ai_row:
            ai_questions = ai_row["suggested_questions"]
    progress = get_evaluatee_progress(db, detail["id"]) if detail else None
    return render_template_string(COMMON_STYLE + NAV_EVALUATOR + """
    <h1>평가자 화면</h1>
    <p class="subtitle">{{user.name}} · {{user.team or ''}} · {{user.email}}</p>

    <div class="section">
      <h3 style="margin-top:0;">배정 대상자</h3>
      <table>
        <tr><th>대상자</th><th>팀</th><th>관계</th><th>PT</th><th>액션</th></tr>
        {% for a in assignments %}
        <tr {% if selected == a.evaluatee_id|string %}style="background:var(--primary-light);"{% endif %}>
          <td><b>{{a.target_name}}</b></td>
          <td>{{a.target_team or '-'}}</td>
          <td><span class="badge badge-info">{{rel_labels.get(a.relationship, a.relationship)}}</span></td>
          <td>{% if a.presentation_filename %}업로드됨{% else %}-{% endif %}</td>
          <td><a href="{{url_for('evaluator_dashboard', evaluatee_id=a.evaluatee_id)}}" class="btn btn-outline btn-sm">평가하기</a></td>
        </tr>
        {% endfor %}
      </table>
    </div>

    {% if detail %}
    <div class="section">
      <h3 style="margin-top:0;">진행 현황</h3>
      <div class="stat-grid">
        <div class="stat-card"><div class="stat-value">{{progress.status}}</div><div class="stat-label">현재 단계</div></div>
        <div class="stat-card"><div class="stat-value">{{'완료' if progress.self_done else '대기'}}</div><div class="stat-label">자가평가</div></div>
        <div class="stat-card"><div class="stat-value">{{progress.evaluator_done_count}}/{{progress.assigned_count}}</div><div class="stat-label">평가자 제출</div></div>
        <div class="stat-card"><div class="stat-value">{{'완료' if progress.delivered else '미전달'}}</div><div class="stat-label">최종 전달</div></div>
      </div>
    </div>
    <div class="section">
      <h3 style="margin-top:0;">{{detail.target_name}} <span style="font-weight:400;color:var(--gray-400);">{{detail.target_team or ''}}</span></h3>
      <p style="margin-bottom:12px;">PT 파일: {% if detail.presentation_filename %}<a href="{{url_for('presentation_file', evaluatee_id=detail.id)}}">{{detail.presentation_filename}}</a>{% else %}미업로드{% endif %}</p>
      <p style="margin-bottom:16px;font-size:13px;color:var(--gray-500);">동료피드백 요약: {{peer_summary}}</p>

      {% if self_data %}
      <h3>자가평가 조회</h3>
      {% for s in self_data %}
      <div class="card">
        <b>{{s.title}}</b> — <span class="badge badge-info">{{s.grade}}</span>
        <div style="font-size:13px;color:var(--gray-600);margin-top:8px;">
          <b>Keep:</b> {{s.keep_text or '-'}}<br/><b>Problem:</b> {{s.problem_text or '-'}}<br/><b>Try:</b> {{s.try_text or '-'}}
        </div>
      </div>
      {% endfor %}
      {% endif %}

      <h3>리더 평가</h3>
      <form method="post">
        <input type="hidden" name="evaluatee_id" value="{{detail.id}}"/>
        {% for item in items %}
        {% set current = leader_data[item.id].grade if item.id in leader_data else 'B' %}
        <div class="card">
          <b>{{item.title}}</b>
          <p style="font-size:13px;color:var(--gray-500);margin:4px 0 10px;">{{item.prompt}}</p>
          <div class="grade-selector">
            {% for g in ['S','A','B','C'] %}
            <div class="grade-option">
              <input type="radio" name="grade_{{item.id}}" value="{{g}}" id="lg_{{item.id}}_{{g}}" {% if g==current %}checked{% endif %}>
              <label for="lg_{{item.id}}_{{g}}">{{g}}
                <span class="grade-desc">{% if g=='S' %}{{item.grade_s}}{% elif g=='A' %}{{item.grade_a}}{% elif g=='B' %}{{item.grade_b}}{% else %}{{item.grade_c}}{% endif %}</span>
              </label>
            </div>
            {% endfor %}
          </div>
        </div>
        {% endfor %}
        <div class="form-group"><label>PT 메모</label><textarea name="presentation_note" rows="3">{{ leader_data.values()|list|first.presentation_note if leader_data else '' }}</textarea></div>
        <div class="form-group"><label>Q&A 메모</label><textarea name="qa_note" rows="3">{{ leader_data.values()|list|first.qa_note if leader_data else '' }}</textarea></div>
        <div class="form-group"><label>대상자 전달 피드백</label><textarea name="feedback_text" rows="4">{{ leader_data.values()|list|first.feedback_text if leader_data else '' }}</textarea></div>
        <button type="submit" class="btn btn-primary">저장</button>
      </form>
    </div>

    <div class="section">
      <h3 style="margin-top:0;">AI 질문 생성</h3>
      <form method="post" action="{{url_for('ai_questions', evaluatee_id=detail.id)}}">
        <div class="form-group"><textarea name="source_note" rows="4" placeholder="발표/질의응답 메모를 입력하면 질문안을 생성합니다."></textarea></div>
        <button type="submit" class="btn btn-outline">질문 생성</button>
      </form>
      {% if ai_questions %}<pre style="white-space:pre-wrap;background:var(--gray-50);padding:14px;border-radius:8px;margin-top:12px;font-size:13px;">{{ai_questions}}</pre>{% endif %}
    </div>
    {% endif %}
    """ + FOOTER, assignments=assignments, detail=detail, self_data=self_data,
        items=items, leader_data=leader_data, peer_summary=peer_summary,
        ai_questions=ai_questions, user=user, selected=selected,
        progress=progress,
        rel_labels=RELATIONSHIP_OPTIONS)


@app.route("/evaluator/<int:evaluatee_id>/ai-questions", methods=["POST"])
@require_role("evaluator")
def ai_questions(evaluatee_id):
    db = get_db()
    user = current_user()
    source_note = request.form.get("source_note", "").strip()
    if not source_note:
        return redirect(url_for("evaluator_dashboard", evaluatee_id=evaluatee_id))
    suggested = build_ai_questions(source_note)
    db.execute("INSERT INTO ai_question_logs(evaluatee_id,evaluator_user_id,source_note,suggested_questions,created_at) VALUES (?,?,?,?,?)",
               (evaluatee_id, user["id"], source_note, suggested, now()))
    log_action(db, "ai_questions_generated", evaluatee_id=evaluatee_id, actor_user_id=user["id"], detail="AI 질문 생성")
    db.commit()
    return redirect(url_for("evaluator_dashboard", evaluatee_id=evaluatee_id))


# ---------------------------------------------------------------------------
# Peer survey
# ---------------------------------------------------------------------------

@app.route("/peer-feedback/<token>", methods=["GET", "POST"])
@app.route("/peer-survey/<token>", methods=["GET", "POST"])
def peer_feedback(token):
    db = get_db()
    evaluatee = db.execute("SELECT e.id, u.name AS target_name FROM evaluatees e JOIN users u ON u.id=e.user_id WHERE e.peer_survey_token=?", (token,)).fetchone()
    if not evaluatee:
        abort(404)
    peers = get_assigned_peer_reviewers(db, evaluatee["id"])
    if request.method == "POST":
        comment = request.form.get("peer_comment", "").strip()
        peer_id = request.form.get("peer_id", "").strip()
        peer_name = request.form.get("peer_name", "").strip()
        if peer_id:
            peer_row = db.execute("SELECT name FROM peer_reviewers WHERE id=?", (peer_id,)).fetchone()
            if peer_row:
                peer_name = peer_row["name"]
        if comment:
            db.execute("INSERT INTO peer_surveys(evaluatee_id,peer_name,peer_comment,created_at) VALUES (?,?,?,?)", (evaluatee["id"], peer_name, comment, now()))
            log_action(db, "peer_feedback_submitted", evaluatee_id=evaluatee["id"], actor_user_id=None, detail=f"동료피드백 제출: {peer_name or '익명'}")
            db.commit()
            return render_template_string(COMMON_STYLE + """
            <div style="text-align:center;margin-top:80px;">
              <div style="font-size:48px;margin-bottom:16px;">✅</div>
              <h2 style="border:none;">제출 완료</h2>
              <p class="subtitle">동료피드백이 성공적으로 제출되었습니다.</p>
            </div>""" + FOOTER)
    return render_template_string(COMMON_STYLE + """
    <div style="max-width:600px;margin:40px auto;">
      <div class="section">
        <h2 style="margin-bottom:4px;">동료피드백 작성</h2>
        <p class="subtitle">대상자: <b>{{evaluatee.target_name}}</b></p>
        <form method="post">
          <div class="form-group">
            <label>동료피드백 참여자 선택</label>
            <select name="peer_id">
              <option value="">-- 선택 --</option>
              {% for p in peers %}<option value="{{p.id}}">{{p.name}}</option>{% endfor %}
            </select>
            {% if not peers %}<p style="font-size:12px;color:var(--warning);margin-top:6px;">아직 배정된 동료피드백 참여자가 없습니다. 관리자에게 배정을 요청해주세요.</p>{% endif %}
          </div>
          <div class="form-group">
            <label>직접 입력 (선택)</label>
            <input type="text" name="peer_name" placeholder="이름"/>
          </div>
          <div class="form-group">
            <label>피드백</label>
            <textarea name="peer_comment" rows="6" required placeholder="대상자에 대한 피드백을 자유롭게 작성해주세요."></textarea>
          </div>
          <button type="submit" class="btn btn-primary" style="width:100%;">제출</button>
        </form>
      </div>
    </div>""" + FOOTER, evaluatee=evaluatee, peers=peers)


# ---------------------------------------------------------------------------
# File access
# ---------------------------------------------------------------------------

@app.route("/uploads/<path:filename>")
def download_upload(filename):
    if not current_user():
        abort(403)
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)


def can_access_evaluatee_file(user, evaluatee):
    if not user or not evaluatee:
        return False
    if user["role"] == "admin":
        return True
    if user["role"] == "target":
        return evaluatee["user_id"] == user["id"]
    if user["role"] == "evaluator":
        row = get_db().execute("SELECT 1 FROM evaluator_assignments WHERE evaluatee_id=? AND evaluator_user_id=?", (evaluatee["id"], user["id"])).fetchone()
        return bool(row)
    return False


@app.route("/presentation/<int:evaluatee_id>")
def presentation_file(evaluatee_id):
    user = current_user()
    if not user:
        abort(403)
    db = get_db()
    evaluatee = db.execute("SELECT * FROM evaluatees WHERE id=?", (evaluatee_id,)).fetchone()
    if not evaluatee or not can_access_evaluatee_file(user, evaluatee):
        abort(403)
    if not evaluatee["presentation_filename"]:
        abort(404)
    storage = evaluatee["presentation_storage"] or "local"
    if storage == "s3" and evaluatee["presentation_file_id"]:
        mime, content = download_from_object_storage(evaluatee["presentation_file_id"])
        if content is None:
            abort(404)
        return Response(content, mimetype=mime, headers={"Content-Disposition": f'inline; filename="{evaluatee["presentation_filename"]}"'})
    if storage == "gdrive" and evaluatee["presentation_file_id"]:
        name, mime, content = download_from_google_drive(evaluatee["presentation_file_id"])
        if content is None:
            abort(404)
        return Response(content, mimetype=mime, headers={"Content-Disposition": f'inline; filename="{name or evaluatee["presentation_filename"]}"'})
    return send_from_directory(UPLOAD_DIR, evaluatee["presentation_filename"], as_attachment=False)


# ---------------------------------------------------------------------------
# Admin: aggregate & deliver
# ---------------------------------------------------------------------------

@app.route("/admin/aggregate/<int:evaluatee_id>")
@require_role("admin")
def aggregate_result(evaluatee_id):
    db = get_db()
    item_rows = db.execute("SELECT id, title FROM assessment_items ORDER BY id").fetchall()
    item_grades = get_item_grades_for_evaluatee(db, evaluatee_id)
    decision = decide_result(item_grades)
    labels = [f"{item['title']}={item_grades.get(item['id'], 'N/A')}" for item in item_rows]
    pv = get_peer_visibility(db)
    peer_text = summarize_peer_comments(db, evaluatee_id) if pv in ("admin_only", "admin_and_evaluator") else "비공개"
    summary = " / ".join(labels) + " / " + peer_text
    db.execute("""INSERT INTO aggregated_results(evaluatee_id,decision,summary,updated_at)
        VALUES (?,?,?,?) ON CONFLICT(evaluatee_id) DO UPDATE SET
        decision=excluded.decision, summary=excluded.summary, updated_at=excluded.updated_at""",
               (evaluatee_id, decision, summary, now()))
    actor = current_user()
    log_action(db, "result_aggregated", evaluatee_id=evaluatee_id, actor_user_id=actor["id"] if actor else None, detail=f"판정: {decision}")
    db.commit()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/deliver/<int:evaluatee_id>", methods=["GET", "POST"])
@require_role("admin")
def deliver_feedback(evaluatee_id):
    db = get_db()
    result = db.execute("SELECT * FROM aggregated_results WHERE evaluatee_id=?", (evaluatee_id,)).fetchone()
    if not result:
        return redirect(url_for("aggregate_result", evaluatee_id=evaluatee_id))
    target_info = db.execute("SELECT u.name FROM evaluatees e JOIN users u ON u.id=e.user_id WHERE e.id=?", (evaluatee_id,)).fetchone()
    target_name = target_info["name"] if target_info else "대상자"

    all_feedbacks = db.execute("""
        SELECT la.evaluator_user_id,
               MAX(la.feedback_text) AS feedback_text,
               MAX(la.updated_at) AS updated_at,
               u.name AS evaluator_name,
               ea.relationship
        FROM leader_assessments la
        JOIN users u ON u.id = la.evaluator_user_id
        JOIN evaluator_assignments ea ON ea.evaluatee_id = la.evaluatee_id AND ea.evaluator_user_id = la.evaluator_user_id
        WHERE la.evaluatee_id = ? AND la.feedback_text IS NOT NULL AND la.feedback_text != ''
        GROUP BY la.evaluator_user_id, u.name, ea.relationship
        ORDER BY MAX(la.updated_at) DESC
    """, (evaluatee_id,)).fetchall()
    raw_feedbacks = "\n\n".join([f"[{RELATIONSHIP_OPTIONS.get(f['relationship'], f['relationship'])} - {f['evaluator_name']}]\n{f['feedback_text']}" for f in all_feedbacks])

    if request.method == "POST":
        action = request.form.get("action", "deliver")
        if action == "ai_polish":
            polished = polish_feedback_with_ai(raw_feedbacks, target_name)
            db.execute("UPDATE aggregated_results SET ai_polished_feedback=?, updated_at=? WHERE evaluatee_id=?", (polished, now(), evaluatee_id))
            actor = current_user()
            log_action(db, "feedback_ai_polished", evaluatee_id=evaluatee_id, actor_user_id=actor["id"] if actor else None, detail="AI 종합 피드백 정리")
            db.commit()
            return redirect(url_for("deliver_feedback", evaluatee_id=evaluatee_id))
        admin_feedback = request.form.get("admin_feedback", "")
        db.execute("UPDATE aggregated_results SET admin_feedback=?, delivered_at=?, updated_at=? WHERE evaluatee_id=?",
                   (admin_feedback, now(), now(), evaluatee_id))
        actor = current_user()
        log_action(db, "feedback_delivered", evaluatee_id=evaluatee_id, actor_user_id=actor["id"] if actor else None, detail="최종 피드백 전달")
        db.commit()
        return redirect(url_for("admin_dashboard"))

    return render_template_string(COMMON_STYLE + NAV_ADMIN + """
    <h1>종합 피드백 전달</h1>
    <p class="subtitle">{{target_name}}에게 전달할 피드백을 작성합니다</p>

    <div class="section">
      <h3 style="margin-top:0;">판정 결과</h3>
      <p>{% set d = result.decision %}
         {% if d == 'SUPER_PASS' %}<span class="badge badge-success">{{dl[d]}}</span>
         {% elif d == 'PASS' %}<span class="badge badge-success">{{dl[d]}}</span>
         {% elif d == 'EXTENSION' %}<span class="badge badge-warning">{{dl[d]}}</span>
         {% elif d == 'FAIL' %}<span class="badge badge-danger">{{dl[d]}}</span>
         {% else %}<span class="badge badge-gray">{{dl[d]}}</span>{% endif %}</p>
      <p style="margin-top:8px;font-size:13px;color:var(--gray-500);">{{result.summary}}</p>
    </div>

    <div class="section">
      <h3 style="margin-top:0;">전체 평가자 피드백 원문</h3>
      {% for f in all_feedbacks %}
      <div class="card">
        <span class="badge badge-info">{{rel_labels.get(f.relationship, f.relationship)}}</span>
        <b style="margin-left:8px;">{{f.evaluator_name}}</b>
        <p style="margin-top:8px;white-space:pre-wrap;">{{f.feedback_text}}</p>
      </div>
      {% endfor %}
      {% if not all_feedbacks %}<p style="color:var(--gray-400);">아직 제출된 피드백이 없습니다.</p>{% endif %}
    </div>

    <div class="section">
      <h3 style="margin-top:0;">AI 피드백 정리</h3>
      <p style="font-size:13px;color:var(--gray-500);margin-bottom:12px;">전체 평가자의 피드백을 AI가 맞춤법 교정, 중복 통합, 구조화하여 정리합니다.</p>
      <form method="post" style="margin-bottom:16px;">
        <input type="hidden" name="action" value="ai_polish"/>
        <button type="submit" class="btn btn-outline">AI로 피드백 정리하기</button>
      </form>
      {% if result.ai_polished_feedback %}
      <div style="white-space:pre-wrap;background:var(--primary-light);padding:18px;border-radius:8px;font-size:14px;line-height:1.7;">{{result.ai_polished_feedback}}</div>
      {% endif %}
    </div>

    <div class="section">
      <h3 style="margin-top:0;">최종 전달 피드백</h3>
      <form method="post">
        <input type="hidden" name="action" value="deliver"/>
        <div class="form-group">
          <label>관리자 피드백 (대상자에게 직접 전달됨)</label>
          <textarea name="admin_feedback" rows="8">{{result.ai_polished_feedback or result.admin_feedback or ''}}</textarea>
        </div>
        <button type="submit" class="btn btn-success">대상자에게 전달</button>
      </form>
    </div>
    """ + FOOTER, result=result, target_name=target_name, all_feedbacks=all_feedbacks,
        dl=DECISION_LABELS, rel_labels=RELATIONSHIP_OPTIONS)


# ---------------------------------------------------------------------------
# Admin: Report
# ---------------------------------------------------------------------------

@app.route("/admin/report/<int:evaluatee_id>")
@require_role("admin")
def admin_report(evaluatee_id):
    db = get_db()
    evaluatee = db.execute("SELECT e.*, u.name AS target_name, u.team AS target_team, u.email AS target_email, c.name AS cycle_name FROM evaluatees e JOIN users u ON u.id=e.user_id JOIN evaluation_cycles c ON c.id=e.cycle_id WHERE e.id=?", (evaluatee_id,)).fetchone()
    if not evaluatee:
        abort(404)
    items = db.execute("SELECT * FROM assessment_items ORDER BY id").fetchall()
    self_data = db.execute("SELECT sa.*, ai.title, ai.code FROM self_assessments sa JOIN assessment_items ai ON ai.id=sa.item_id WHERE sa.evaluatee_id=? ORDER BY ai.id", (evaluatee_id,)).fetchall()
    leader_rows = db.execute("""SELECT la.*, u.name AS evaluator_name, ai.title AS item_title, ea.relationship
        FROM leader_assessments la JOIN users u ON u.id=la.evaluator_user_id
        JOIN assessment_items ai ON ai.id=la.item_id
        JOIN evaluator_assignments ea ON ea.evaluatee_id=la.evaluatee_id AND ea.evaluator_user_id=la.evaluator_user_id
        WHERE la.evaluatee_id=? ORDER BY la.evaluator_user_id, ai.id""", (evaluatee_id,)).fetchall()
    peer_surveys = db.execute("SELECT * FROM peer_surveys WHERE evaluatee_id=? ORDER BY id", (evaluatee_id,)).fetchall()
    result = db.execute("SELECT * FROM aggregated_results WHERE evaluatee_id=?", (evaluatee_id,)).fetchone()
    item_grades = get_item_grades_for_evaluatee(db, evaluatee_id)
    evaluators_info = db.execute("""SELECT u.name, u.team, ea.relationship FROM evaluator_assignments ea
        JOIN users u ON u.id=ea.evaluator_user_id WHERE ea.evaluatee_id=?""", (evaluatee_id,)).fetchall()
    progress = get_evaluatee_progress(db, evaluatee_id)
    timeline = get_audit_logs_for_evaluatee(db, evaluatee_id, limit=30)

    return render_template_string(COMMON_STYLE + NAV_ADMIN + """
    <div class="no-print" style="margin-bottom:16px;">
      <button onclick="window.print()" class="btn btn-primary">리포트 인쇄 / PDF 저장</button>
      <a href="{{url_for('admin_dashboard')}}" class="btn btn-outline" style="margin-left:8px;">돌아가기</a>
    </div>

    <div class="section">
      <h1 style="text-align:center;margin-bottom:4px;">수습평가 최종 리포트</h1>
      <p style="text-align:center;color:var(--gray-500);">{{evaluatee.cycle_name}}</p>
    </div>

    <div class="section">
      <h3 style="margin-top:0;">대상자 정보</h3>
      <table>
        <tr><th style="width:140px;">이름</th><td>{{evaluatee.target_name}}</td><th style="width:140px;">팀</th><td>{{evaluatee.target_team or '-'}}</td></tr>
        <tr><th>이메일</th><td>{{evaluatee.target_email}}</td><th>자가평가 제출</th><td>{{evaluatee.self_submitted_at or '미제출'}}</td></tr>
        <tr><th>현재 단계</th><td>{{progress.status}}</td><th>평가자 제출</th><td>{{progress.evaluator_done_count}} / {{progress.assigned_count}}</td></tr>
      </table>
    </div>

    <div class="section">
      <h3 style="margin-top:0;">평가자 정보</h3>
      <table>
        <tr><th>이름</th><th>팀</th><th>관계</th></tr>
        {% for ev in evaluators_info %}
        <tr><td>{{ev.name}}</td><td>{{ev.team or '-'}}</td><td>{{rel_labels.get(ev.relationship, ev.relationship)}}</td></tr>
        {% endfor %}
      </table>
    </div>

    <div class="section">
      <h3 style="margin-top:0;">자가평가</h3>
      {% for s in self_data %}
      <div class="card">
        <b>{{s.title}}</b> — <span class="badge badge-info">{{s.grade}}</span>
        <div style="font-size:13px;margin-top:6px;"><b>Keep:</b> {{s.keep_text or '-'}}</div>
        <div style="font-size:13px;"><b>Problem:</b> {{s.problem_text or '-'}}</div>
        <div style="font-size:13px;"><b>Try:</b> {{s.try_text or '-'}}</div>
      </div>
      {% endfor %}
    </div>

    <div class="section">
      <h3 style="margin-top:0;">리더 평가 상세</h3>
      {% for la in leader_rows %}
      <div class="card">
        <span class="badge badge-info">{{rel_labels.get(la.relationship, la.relationship)}}</span>
        <b style="margin-left:8px;">{{la.evaluator_name}}</b> — <b>{{la.item_title}}</b>: <span class="badge badge-info">{{la.grade}}</span>
        {% if la.feedback_text %}<p style="font-size:13px;margin-top:6px;">{{la.feedback_text}}</p>{% endif %}
      </div>
      {% endfor %}
    </div>

    <div class="section">
      <h3 style="margin-top:0;">종합 등급</h3>
      <table>
        <tr><th>평가항목</th><th>종합 등급</th></tr>
        {% for item in items %}
        <tr><td>{{item.title}}</td><td><span class="badge badge-info">{{item_grades.get(item.id, 'N/A')}}</span></td></tr>
        {% endfor %}
      </table>
    </div>

    <div class="section">
      <h3 style="margin-top:0;">동료피드백</h3>
      {% for ps in peer_surveys %}
      <div class="card"><b>{{ps.peer_name or '익명'}}</b><p style="margin-top:4px;">{{ps.peer_comment}}</p></div>
      {% endfor %}
      {% if not peer_surveys %}<p style="color:var(--gray-400);">수집된 동료 의견이 없습니다.</p>{% endif %}
    </div>

    {% if result %}
    <div class="section">
      <h3 style="margin-top:0;">최종 판정</h3>
      <p style="font-size:20px;font-weight:700;">
        {% set d = result.decision %}
        {% if d == 'SUPER_PASS' %}<span class="badge badge-success" style="font-size:16px;padding:8px 20px;">{{dl[d]}}</span>
        {% elif d == 'PASS' %}<span class="badge badge-success" style="font-size:16px;padding:8px 20px;">{{dl[d]}}</span>
        {% elif d == 'EXTENSION' %}<span class="badge badge-warning" style="font-size:16px;padding:8px 20px;">{{dl[d]}}</span>
        {% elif d == 'FAIL' %}<span class="badge badge-danger" style="font-size:16px;padding:8px 20px;">{{dl[d]}}</span>
        {% else %}<span class="badge badge-gray" style="font-size:16px;padding:8px 20px;">{{dl[d]}}</span>{% endif %}
      </p>
      {% if result.ai_polished_feedback or result.admin_feedback %}
      <h3>전달 피드백</h3>
      <div style="white-space:pre-wrap;background:var(--gray-50);padding:18px;border-radius:8px;line-height:1.7;">{{result.ai_polished_feedback or result.admin_feedback}}</div>
      {% endif %}
    </div>
    {% endif %}

    <div class="section">
      <h3 style="margin-top:0;">운영 타임라인</h3>
      {% for log in timeline %}
      <div class="card">
        <b>{{log.created_at}}</b>
        <span class="badge badge-info" style="margin-left:8px;">{{log.action}}</span>
        <div style="font-size:13px;color:var(--gray-600);margin-top:6px;">{{log.actor_name or '시스템'}}{% if log.detail %} · {{log.detail}}{% endif %}</div>
      </div>
      {% endfor %}
      {% if not timeline %}<p style="color:var(--gray-400);">기록된 타임라인이 없습니다.</p>{% endif %}
    </div>

    <p style="text-align:center;color:var(--gray-400);font-size:12px;margin-top:32px;">생성일: {{now}} | 수습평가 시스템</p>
    """ + FOOTER, evaluatee=evaluatee, items=items, self_data=self_data,
        leader_rows=leader_rows, peer_surveys=peer_surveys, result=result,
        item_grades=item_grades, evaluators_info=evaluators_info, progress=progress, timeline=timeline,
        dl=DECISION_LABELS, rel_labels=RELATIONSHIP_OPTIONS, now=now())


@app.route("/admin/peer-feedback/<int:evaluatee_id>", methods=["GET", "POST"])
@require_role("admin")
def manage_peer_feedback_assignments(evaluatee_id):
    db = get_db()
    evaluatee = db.execute(
        """
        SELECT e.id, u.name AS target_name, u.team AS target_team, c.name AS cycle_name
        FROM evaluatees e
        JOIN users u ON u.id = e.user_id
        JOIN evaluation_cycles c ON c.id = e.cycle_id
        WHERE e.id=?
        """,
        (evaluatee_id,),
    ).fetchone()
    if not evaluatee:
        abort(404)
    peer_reviewers = db.execute("SELECT * FROM peer_reviewers ORDER BY name").fetchall()
    if request.method == "POST":
        selected_ids = set(request.form.getlist("peer_reviewer_ids"))
        db.execute("DELETE FROM peer_feedback_assignments WHERE evaluatee_id=?", (evaluatee_id,))
        for peer in peer_reviewers:
            if str(peer["id"]) in selected_ids:
                db.execute(
                    "INSERT INTO peer_feedback_assignments(evaluatee_id,peer_reviewer_id) VALUES (?,?)",
                    (evaluatee_id, peer["id"]),
                )
        actor = current_user()
        log_action(
            db,
            "peer_feedback_assignment_updated",
            evaluatee_id=evaluatee_id,
            actor_user_id=actor["id"] if actor else None,
            detail=f"배정 인원 {len(selected_ids)}명",
        )
        db.commit()
        return redirect(url_for("manage_peer_feedback_assignments", evaluatee_id=evaluatee_id))
    assigned = {row["id"] for row in get_assigned_peer_reviewers(db, evaluatee_id)}
    return render_template_string(
        COMMON_STYLE + NAV_ADMIN + """
        <h1>동료피드백 참여자 배정</h1>
        <p class="subtitle">{{evaluatee.target_name}} · {{evaluatee.target_team or '-'}} · {{evaluatee.cycle_name}}</p>

        <div class="section">
          <h3 style="margin-top:0;">배정할 참여자 선택</h3>
          <form method="post">
            <div class="form-row">
              {% for p in peer_reviewers %}
              <label style="display:flex;align-items:center;gap:8px;font-weight:500;">
                <input type="checkbox" name="peer_reviewer_ids" value="{{p.id}}" {% if p.id in assigned %}checked{% endif %}>
                {{p.name}}
              </label>
              {% endfor %}
            </div>
            <div style="margin-top:16px;">
              <button type="submit" class="btn btn-primary">배정 저장</button>
              <a href="{{url_for('admin_dashboard')}}" class="btn btn-outline" style="margin-left:8px;">돌아가기</a>
            </div>
          </form>
        </div>
        """,
        evaluatee=evaluatee,
        peer_reviewers=peer_reviewers,
        assigned=assigned,
    ) + FOOTER


# ---------------------------------------------------------------------------
# Admin: User / Cycle / Peer management
# ---------------------------------------------------------------------------

@app.route("/admin/users", methods=["GET", "POST"])
@require_role("admin")
def manage_users():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip()
            role = request.form.get("role", "target")
            password = request.form.get("password", "").strip()
            team = request.form.get("team", "").strip()
            access_start = request.form.get("access_start", "").strip() or None
            access_end = request.form.get("access_end", "").strip() or None
            if name and email and role in ("admin", "target", "evaluator"):
                if not password:
                    password = {"target": "target1234", "evaluator": "leader1234", "admin": "admin1234"}.get(role, "pass1234")
                db.execute("INSERT INTO users(name, email, role, password_hash, team, access_start, access_end) VALUES (?,?,?,?,?,?,?)",
                           (name, email, role, generate_password_hash(password), team, access_start, access_end))
                actor = current_user()
                log_action(db, "user_added", actor_user_id=actor["id"] if actor else None, detail=f"{name} / {role}")
                db.commit()
        elif action == "update_access":
            user_id = request.form.get("user_id")
            access_start = request.form.get("access_start", "").strip() or None
            access_end = request.form.get("access_end", "").strip() or None
            team = request.form.get("team", "").strip()
            if user_id:
                db.execute("UPDATE users SET access_start=?, access_end=?, team=? WHERE id=?", (access_start, access_end, team, user_id))
                actor = current_user()
                log_action(db, "user_access_updated", actor_user_id=actor["id"] if actor else None, detail=f"user_id={user_id}")
                db.commit()
        elif action == "reset_password":
            user_id = request.form.get("user_id")
            new_password = request.form.get("new_password", "").strip()
            if user_id and new_password:
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password), user_id))
                actor = current_user()
                log_action(db, "user_password_reset", actor_user_id=actor["id"] if actor else None, detail=f"user_id={user_id}")
                db.commit()
        elif action == "delete":
            user_id = request.form.get("user_id")
            if user_id:
                db.execute("DELETE FROM users WHERE id=?", (user_id,))
                actor = current_user()
                log_action(db, "user_deleted", actor_user_id=actor["id"] if actor else None, detail=f"user_id={user_id}")
                db.commit()
        return redirect(url_for("manage_users"))
    users = db.execute("SELECT * FROM users ORDER BY role, id").fetchall()
    return render_template_string(COMMON_STYLE + NAV_ADMIN + """
    <h1>사용자 관리</h1>
    <p class="subtitle">사용자 추가, 접근기간 설정, 팀 배정을 관리합니다</p>

    <div class="section">
      <h3 style="margin-top:0;">사용자 추가</h3>
      <form method="post">
        <input type="hidden" name="action" value="add"/>
        <div class="form-row">
          <div class="form-group"><label>이름</label><input type="text" name="name" required/></div>
          <div class="form-group"><label>이메일</label><input type="email" name="email" required/></div>
          <div class="form-group"><label>역할</label>
            <select name="role"><option value="target">평가대상자</option><option value="evaluator">평가자</option><option value="admin">관리자</option></select>
          </div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>팀</label><input type="text" name="team" placeholder="예: 개발팀"/></div>
          <div class="form-group"><label>초기 비밀번호</label><input type="text" name="password" placeholder="미입력 시 기본값"/></div>
          <div class="form-group"><label>접근 시작일</label><input type="date" name="access_start"/></div>
          <div class="form-group"><label>접근 종료일</label><input type="date" name="access_end"/></div>
        </div>
        <button type="submit" class="btn btn-primary">추가</button>
      </form>
    </div>

    <div class="section">
      <h3 style="margin-top:0;">사용자 목록</h3>
      <table>
        <tr><th>이름</th><th>이메일</th><th>역할</th><th>팀</th><th>접근기간</th><th>액션</th></tr>
        {% for u in users %}
        <tr>
          <td><b>{{u.name}}</b></td>
          <td style="font-size:13px;">{{u.email}}</td>
          <td>{% if u.role=='admin' %}<span class="badge badge-danger">관리자</span>{% elif u.role=='target' %}<span class="badge badge-info">대상자</span>{% else %}<span class="badge badge-warning">평가자</span>{% endif %}</td>
          <td>{{u.team or '-'}}</td>
          <td style="font-size:12px;">{{u.access_start or '∞'}} ~ {{u.access_end or '∞'}}</td>
          <td style="white-space:nowrap;">
            <form method="post" style="display:inline;">
              <input type="hidden" name="action" value="update_access"/>
              <input type="hidden" name="user_id" value="{{u.id}}"/>
              <input type="text" name="team" value="{{u.team or ''}}" placeholder="팀" style="width:70px;padding:4px 6px;font-size:12px;"/>
              <input type="date" name="access_start" value="{{u.access_start or ''}}" style="width:120px;padding:4px 6px;font-size:12px;"/>
              <input type="date" name="access_end" value="{{u.access_end or ''}}" style="width:120px;padding:4px 6px;font-size:12px;"/>
              <button type="submit" class="btn btn-outline btn-sm">저장</button>
            </form>
            <form method="post" style="display:inline;margin-left:4px;">
              <input type="hidden" name="action" value="reset_password"/><input type="hidden" name="user_id" value="{{u.id}}"/>
              <input type="text" name="new_password" placeholder="새 비번" style="width:80px;padding:4px 6px;font-size:12px;" required/>
              <button type="submit" class="btn btn-outline btn-sm">변경</button>
            </form>
            <form method="post" style="display:inline;margin-left:4px;">
              <input type="hidden" name="action" value="delete"/><input type="hidden" name="user_id" value="{{u.id}}"/>
              <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('삭제하시겠습니까?')">삭제</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </table>
    </div>
    """ + FOOTER, users=users)


@app.route("/admin/cycles", methods=["GET", "POST"])
@require_role("admin")
def manage_cycles():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            start_date = request.form.get("start_date", "")
            end_date = request.form.get("end_date", "")
            if name:
                db.execute("INSERT INTO evaluation_cycles(name, start_date, end_date) VALUES (?,?,?)", (name, start_date, end_date))
                actor = current_user()
                log_action(db, "cycle_added", actor_user_id=actor["id"] if actor else None, detail=name)
                db.commit()
        elif action == "delete":
            cycle_id = request.form.get("cycle_id")
            if cycle_id:
                db.execute("DELETE FROM evaluation_cycles WHERE id=?", (cycle_id,))
                actor = current_user()
                log_action(db, "cycle_deleted", actor_user_id=actor["id"] if actor else None, detail=f"cycle_id={cycle_id}")
                db.commit()
        return redirect(url_for("manage_cycles"))
    cycles = db.execute("SELECT * FROM evaluation_cycles ORDER BY id DESC").fetchall()
    return render_template_string(COMMON_STYLE + NAV_ADMIN + """
    <h1>평가 사이클 관리</h1>
    <div class="section">
      <h3 style="margin-top:0;">사이클 추가</h3>
      <form method="post">
        <input type="hidden" name="action" value="add"/>
        <div class="form-row">
          <div class="form-group"><label>사이클명</label><input type="text" name="name" required/></div>
          <div class="form-group"><label>시작일</label><input type="date" name="start_date"/></div>
          <div class="form-group"><label>종료일</label><input type="date" name="end_date"/></div>
        </div>
        <button type="submit" class="btn btn-primary">추가</button>
      </form>
    </div>
    <div class="section">
      <table>
        <tr><th>사이클명</th><th>시작일</th><th>종료일</th><th>상태</th><th>액션</th></tr>
        {% for c in cycles %}
        <tr>
          <td><b>{{c.name}}</b></td><td>{{c.start_date or '-'}}</td><td>{{c.end_date or '-'}}</td><td><span class="badge badge-success">{{c.status}}</span></td>
          <td><form method="post" style="display:inline"><input type="hidden" name="action" value="delete"/><input type="hidden" name="cycle_id" value="{{c.id}}"/>
            <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('삭제하시겠습니까?')">삭제</button></form></td>
        </tr>
        {% endfor %}
      </table>
    </div>
    """ + FOOTER, cycles=cycles)


@app.route("/admin/peers", methods=["GET", "POST"])
@require_role("admin")
def manage_peers():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            if name:
                db.execute("INSERT INTO peer_reviewers(name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
                actor = current_user()
                log_action(db, "peer_reviewer_added", actor_user_id=actor["id"] if actor else None, detail=name)
                db.commit()
        elif action == "delete":
            peer_id = request.form.get("peer_id")
            if peer_id:
                db.execute("DELETE FROM peer_reviewers WHERE id=?", (peer_id,))
                actor = current_user()
                log_action(db, "peer_reviewer_deleted", actor_user_id=actor["id"] if actor else None, detail=f"peer_id={peer_id}")
                db.commit()
        return redirect(url_for("manage_peers"))
    peers = db.execute("SELECT * FROM peer_reviewers ORDER BY id").fetchall()
    return render_template_string(COMMON_STYLE + NAV_ADMIN + """
    <h1>동료피드백 참여자 관리</h1>
    <div class="section">
      <form method="post" class="form-row" style="align-items:end;">
        <input type="hidden" name="action" value="add"/>
        <div class="form-group" style="flex:2;"><label>이름</label><input type="text" name="name" required/></div>
        <div class="form-group" style="flex:0;"><button type="submit" class="btn btn-primary">추가</button></div>
      </form>
    </div>
    <div class="section">
      <table>
        <tr><th>이름</th><th>액션</th></tr>
        {% for p in peers %}
        <tr>
          <td>{{p.name}}</td>
          <td><form method="post" style="display:inline"><input type="hidden" name="action" value="delete"/><input type="hidden" name="peer_id" value="{{p.id}}"/>
            <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('삭제하시겠습니까?')">삭제</button></form></td>
        </tr>
        {% endfor %}
      </table>
    </div>
    """ + FOOTER, peers=peers)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return {"status": "ok", "time": now()}


if __name__ == "__main__":
    ensure_db_initialized()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
