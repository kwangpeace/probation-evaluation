# -*- coding: utf-8 -*-
import os
import secrets
import sqlite3
from io import BytesIO
from datetime import datetime
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
    "evaluator_only": "\ud3c9\uac00\uc790\ub9cc \uacf5\uac1c",
    "admin_only": "\uad00\ub9ac\uc790\ub9cc \uacf5\uac1c",
    "admin_and_evaluator": "\uad00\ub9ac\uc790+\ud3c9\uac00\uc790 \uacf5\uac1c",
}


def now():
    return datetime.now().isoformat(timespec="seconds")


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
        import json

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
    client.put_object(
        Bucket=OBJECT_STORAGE_BUCKET,
        Key=object_key,
        Body=file_bytes,
        ContentType=content_type or "application/octet-stream",
    )
    return True


def download_from_object_storage(object_key):
    client = get_object_storage_client()
    if client is None or not object_key:
        return None, None
    resp = client.get_object(Bucket=OBJECT_STORAGE_BUCKET, Key=object_key)
    return resp.get("ContentType") or "application/octet-stream", resp["Body"].read()


def scalar_count(db, table_name):
    row = db.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
    if row is None:
        return 0
    if isinstance(row, dict):
        return int(row.get("count", 0))
    if hasattr(row, "keys") and "count" in row.keys():
        return int(row["count"])
    return int(row[0])


def get_db():
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = connect_db()
    if USE_POSTGRES:
        schema_script = """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL CHECK (role IN ('admin', 'target', 'evaluator'))
        );
        CREATE TABLE IF NOT EXISTS evaluation_cycles (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS evaluatees (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            cycle_id INTEGER NOT NULL,
            peer_survey_token TEXT NOT NULL UNIQUE,
            presentation_filename TEXT,
            presentation_file_id TEXT,
            presentation_storage TEXT NOT NULL DEFAULT 'local',
            self_submitted_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evaluator_assignments (
            id SERIAL PRIMARY KEY,
            evaluatee_id INTEGER NOT NULL,
            evaluator_user_id INTEGER NOT NULL,
            UNIQUE(evaluatee_id, evaluator_user_id)
        );
        CREATE TABLE IF NOT EXISTS assessment_items (
            id SERIAL PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            grade_s TEXT NOT NULL,
            grade_a TEXT NOT NULL,
            grade_b TEXT NOT NULL,
            grade_c TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS self_assessments (
            id SERIAL PRIMARY KEY,
            evaluatee_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            grade TEXT NOT NULL,
            keep_text TEXT,
            problem_text TEXT,
            try_text TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(evaluatee_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS leader_assessments (
            id SERIAL PRIMARY KEY,
            evaluatee_id INTEGER NOT NULL,
            evaluator_user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            grade TEXT NOT NULL,
            feedback_text TEXT,
            presentation_note TEXT,
            qa_note TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(evaluatee_id, evaluator_user_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS peer_surveys (
            id SERIAL PRIMARY KEY,
            evaluatee_id INTEGER NOT NULL,
            peer_name TEXT,
            peer_comment TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS aggregated_results (
            id SERIAL PRIMARY KEY,
            evaluatee_id INTEGER NOT NULL UNIQUE,
            decision TEXT NOT NULL,
            summary TEXT NOT NULL,
            admin_feedback TEXT,
            delivered_at TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_question_logs (
            id SERIAL PRIMARY KEY,
            evaluatee_id INTEGER NOT NULL,
            evaluator_user_id INTEGER NOT NULL,
            source_note TEXT NOT NULL,
            suggested_questions TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS peer_reviewers (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );
        """
    else:
        schema_script = """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL CHECK (role IN ('admin', 'target', 'evaluator'))
        );
        CREATE TABLE IF NOT EXISTS evaluation_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS evaluatees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            cycle_id INTEGER NOT NULL,
            peer_survey_token TEXT NOT NULL UNIQUE,
            presentation_filename TEXT,
            presentation_file_id TEXT,
            presentation_storage TEXT NOT NULL DEFAULT 'local',
            self_submitted_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evaluator_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluatee_id INTEGER NOT NULL,
            evaluator_user_id INTEGER NOT NULL,
            UNIQUE(evaluatee_id, evaluator_user_id)
        );
        CREATE TABLE IF NOT EXISTS assessment_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            grade_s TEXT NOT NULL,
            grade_a TEXT NOT NULL,
            grade_b TEXT NOT NULL,
            grade_c TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS self_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluatee_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            grade TEXT NOT NULL,
            keep_text TEXT,
            problem_text TEXT,
            try_text TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(evaluatee_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS leader_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluatee_id INTEGER NOT NULL,
            evaluator_user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            grade TEXT NOT NULL,
            feedback_text TEXT,
            presentation_note TEXT,
            qa_note TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(evaluatee_id, evaluator_user_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS peer_surveys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluatee_id INTEGER NOT NULL,
            peer_name TEXT,
            peer_comment TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS aggregated_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluatee_id INTEGER NOT NULL UNIQUE,
            decision TEXT NOT NULL,
            summary TEXT NOT NULL,
            admin_feedback TEXT,
            delivered_at TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_question_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluatee_id INTEGER NOT NULL,
            evaluator_user_id INTEGER NOT NULL,
            source_note TEXT NOT NULL,
            suggested_questions TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS peer_reviewers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        """
    db.executescript(schema_script)
    # Runtime-safe migrations for existing databases.
    if USE_POSTGRES:
        db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT")
        db.execute("ALTER TABLE evaluatees ADD COLUMN IF NOT EXISTS presentation_file_id TEXT")
        db.execute("ALTER TABLE evaluatees ADD COLUMN IF NOT EXISTS presentation_storage TEXT NOT NULL DEFAULT 'local'")
    else:
        user_cols = db.execute("PRAGMA table_info(users)").fetchall()
        user_col_names = {c["name"] if hasattr(c, "keys") else c[1] for c in user_cols}
        if "password_hash" not in user_col_names:
            db.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        eval_cols = db.execute("PRAGMA table_info(evaluatees)").fetchall()
        eval_col_names = {c["name"] if hasattr(c, "keys") else c[1] for c in eval_cols}
        if "presentation_file_id" not in eval_col_names:
            db.execute("ALTER TABLE evaluatees ADD COLUMN presentation_file_id TEXT")
        if "presentation_storage" not in eval_col_names:
            db.execute("ALTER TABLE evaluatees ADD COLUMN presentation_storage TEXT NOT NULL DEFAULT 'local'")

    if scalar_count(db, "users") == 0:
        db.executemany(
            "INSERT INTO users(name, email, role, password_hash) VALUES (?, ?, ?, ?)",
            [
                ("HR \uad00\ub9ac\uc790", "admin@company.local", "admin", generate_password_hash("admin1234")),
                ("\ub300\uc0c1\uc790 \uae40\uc218\uc2b5", "target1@company.local", "target", generate_password_hash("target1234")),
                ("\ub300\uc0c1\uc790 \ubc15\uc218\uc2b5", "target2@company.local", "target", generate_password_hash("target1234")),
                ("\ud3c9\uac00\uc790 \uc774\ub9ac\ub354", "leader1@company.local", "evaluator", generate_password_hash("leader1234")),
                ("\ud3c9\uac00\uc790 \ucd5c\ub9ac\ub354", "leader2@company.local", "evaluator", generate_password_hash("leader1234")),
                ("\ud3c9\uac00\uc790 \uc815\ub9ac\ub354", "leader3@company.local", "evaluator", generate_password_hash("leader1234")),
            ],
        )
    else:
        # Backfill password hash for old rows.
        db.execute(
            "UPDATE users SET password_hash=? WHERE password_hash IS NULL AND role='admin'",
            (generate_password_hash("admin1234"),),
        )
        db.execute(
            "UPDATE users SET password_hash=? WHERE password_hash IS NULL AND role='target'",
            (generate_password_hash("target1234"),),
        )
        db.execute(
            "UPDATE users SET password_hash=? WHERE password_hash IS NULL AND role='evaluator'",
            (generate_password_hash("leader1234"),),
        )
    if scalar_count(db, "evaluation_cycles") == 0:
        db.execute(
            "INSERT INTO evaluation_cycles(name, start_date, end_date) VALUES (?, ?, ?)",
            ("2026\ub144 1\ubd84\uae30 \uc218\uc2b5\ud3c9\uac00", "2026-01-01", "2026-03-31"),
        )
    if scalar_count(db, "assessment_items") == 0:
        db.executemany(
            "INSERT INTO assessment_items(code,title,prompt,grade_s,grade_a,grade_b,grade_c) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    "TEAM_CONTRIBUTION",
                    "\ud300\ubaa9\ud45c \uae30\uc5ec\ub3c4",
                    "\uc785\uc0ac \uc2dc \ud569\uc758\ub41c \uc6b0\ub9ac \ud300\uc758 \ub2f9\uba74 \uacfc\uc81c \ud574\uacb0\uc5d0 \ubcf8\uc778\uc758 \uc5c5\ubb34\uac00 \uc2e4\uc81c\ub85c \uae30\uc5ec\ud588\uc2b5\ub2c8\uae4c?",
                    "\ud575\uc2ec\ubb38\uc81c \ud574\uacb0 \ub610\ub294 \uc5ed\ud560 \ubc94\uc704\ub97c \ub118\uc5b4 \ud300 \ubaa9\ud45c \ub2ec\uc131\uc5d0 \uacb0\uc815\uc801 \uae30\uc5ec",
                    "\ud569\uc758\ub41c \uc5ed\ud560 \ub0b4 \uc784\ubb34\ub97c \ucda9\uc2e4\ud788 \uc218\ud589\ud574 \ud300 \uacfc\uc81c \ud574\uacb0\uc5d0 \uae30\uc5ec",
                    "\uc5c5\ubb34 \uc218\ud589\uc740 \ud588\uc73c\ub098 \uc8fc\ub3c4\uc131\uc774 \ubd80\uc871\ud574 \uc9c0\uc18d \uac00\uc774\ub4dc \ud544\uc694",
                    "\ud300 \uacfc\uc81c\uc640 \ubb34\uad00\ud55c \uc5c5\ubb34 \ub610\ub294 \uacb0\uacfc\ubb3c \ud488\uc9c8 \ubbf8\ub2ec\ub85c \ud300\uc5d0 \ubd80\ub2f4",
                ),
                (
                    "TASK_ACHIEVEMENT",
                    "\ud575\uc2ec\uacfc\uc81c \ub2ec\uc131\ub3c4",
                    "\ud569\uc758\uc11c\uc5d0 \uba85\uc2dc\ub41c 3\uac1c\uc6d4 \ub0b4 \uae30\ub300\uc131\uacfc\ub97c \uc815\uc131/\uc815\ub7c9\uc801\uc73c\ub85c \ub2ec\uc131\ud588\uc2b5\ub2c8\uae4c?",
                    "\ubaa9\ud45c 120% \uc774\uc0c1 \ub610\ub294 \uae30\ub300 \uc218\uc900\uc744 \ud6e8\uc52c \uc0c1\ud68c",
                    "\ubaa9\ud45c 100% \ub2ec\uc131 \ubc0f \ud569\uc758\ub41c \ud488\uc9c8 \ucda9\uc871",
                    "\ubaa9\ud45c \uc57d 80% \ub2ec\uc131 \ub610\ub294 \uc77c\uc815/\ud488\uc9c8 \ubcf4\uc644 \ud544\uc694",
                    "\ub2ec\uc131\ub960 70% \ubbf8\ub9cc \ub610\ub294 \uc2e4\ubb34 \ud65c\uc6a9\uc774 \uc5b4\ub824\uc6b4 \ud488\uc9c8",
                ),
                (
                    "BEHAVIOR_ALIGNMENT",
                    "\uae30\ub300\ud589\ub3d9 \ubd80\ud569\ub3c4",
                    "\ud569\uc758\uc11c\uc5d0 \uba85\uc2dc\ub41c \uae30\ub300\ud589\ub3d9\uc744 \uc900\uc218\ud558\uc600\uc2b5\ub2c8\uae4c?",
                    "\uc644\ubcbd \uc900\uc218\ub97c \ub118\uc5b4 \ud0c0\uc778 \ubaa8\ubc94 \ub610\ub294 \ub354 \ub098\uc740 \ud589\ub3d9 \uc591\uc2dd \uc81c\uc548",
                    "\ud569\uc758\ub41c \ud589\ub3d9\uac00\uc774\ub4dc\ub97c \uc608\uc678 \uc5c6\uc774 \uc900\uc218",
                    "\ub300\uccb4\ub85c \uc900\uc218\ud588\uc73c\ub098 \ud2b9\uc815 \uc0c1\ud669\uc5d0\uc11c \ud589\ub3d9 \uad50\uc815 \ud544\uc694",
                    "\ud589\ub3d9\uae30\uc900 \ubc18\ubcf5 \uc704\ubc18 \ubc0f \uac1c\uc120 \uc694\uccad\uc5d0\ub3c4 \ubcc0\ud654 \ubd80\uc871",
                ),
            ],
        )
    db.execute(
        """
        INSERT INTO app_settings(key, value)
        VALUES ('peer_visibility', 'evaluator_only')
        ON CONFLICT(key) DO NOTHING
        """
    )
    if scalar_count(db, "peer_reviewers") == 0:
        db.executemany(
            "INSERT INTO peer_reviewers(name) VALUES (?)",
            [
                ("\ub3d9\ub8cc\ud3c9\uac00\uc790 \uae40\ub3d9\ub8cc",),
                ("\ub3d9\ub8cc\ud3c9\uac00\uc790 \uc774\ub3d9\ub8cc",),
                ("\ub3d9\ub8cc\ud3c9\uac00\uc790 \ubc15\ub3d9\ub8cc",),
            ],
        )
    db.commit()
    db.close()


def ensure_db_initialized():
    global _db_initialized
    if _db_initialized:
        return
    init_db()
    _db_initialized = True


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def require_role(role=None):
    def deco(fn):
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if role and user["role"] != role:
                abort(403)
            return fn(*args, **kwargs)

        wrapper.__name__ = fn.__name__
        return wrapper

    return deco


def summarize_peer_comments(db, evaluatee_id):
    rows = db.execute(
        "SELECT peer_comment FROM peer_surveys WHERE evaluatee_id = ? ORDER BY id DESC",
        (evaluatee_id,),
    ).fetchall()
    if not rows:
        return "\uc218\uc9d1\ub41c \ub3d9\ub8cc \uc758\uacac\uc774 \uc5c6\uc2b5\ub2c8\ub2e4."
    comments = [r["peer_comment"] for r in rows]
    return "\ucd1d {}\uac74 \uc218\uc9d1. \uc8fc\uc694 \uc758\uacac: {}".format(len(comments), "; ".join(comments[:3]))


def get_peer_visibility(db):
    row = db.execute("SELECT value FROM app_settings WHERE key='peer_visibility'").fetchone()
    if not row:
        return "evaluator_only"
    value = row["value"]
    if value not in PEER_VISIBILITY_OPTIONS:
        return "evaluator_only"
    return value


def get_item_grades_for_evaluatee(db, evaluatee_id):
    rows = db.execute(
        "SELECT item_id, grade FROM leader_assessments WHERE evaluatee_id = ?",
        (evaluatee_id,),
    ).fetchall()
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
    b_count = grades.count("B")
    c_count = grades.count("C")
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
        return (
            "1. \uc774\ubc88 \ubc1c\ud45c\uc758 \ud575\uc2ec \uc131\uacfc\ub97c \uc218\uce58 \uc911\uc2ec\uc73c\ub85c \ub2e4\uc2dc \uc124\uba85\ud574 \uc8fc\uc138\uc694.\n"
            "2. \uac00\uc7a5 \uc5b4\ub824\uc6e0\ub358 \uc758\uc0ac\uacb0\uc815 \uc21c\uac04\uacfc \ud310\ub2e8 \uae30\uc900\uc740 \ubb34\uc5c7\uc774\uc5c8\ub098\uc694?\n"
            "3. \ud611\uc5c5 \uacfc\uc815\uc758 \ubcd1\ubaa9\uc744 \uc5b4\ub5bb\uac8c \ud574\uacb0\ud588\ub098\uc694?\n"
            "4. \uac19\uc740 \uacfc\uc81c\ub97c \ub2e4\uc2dc \uc218\ud589\ud55c\ub2e4\uba74 \uc5b4\ub5a4 \uc810\uc744 \ubc14\uafb8\uaca0\ub098\uc694?\n"
            "5. \ub2e4\uc74c 90\uc77c \ub3d9\uc548\uc758 \ucd5c\uc6b0\uc120 \uac1c\uc120 \ud56d\ubaa9 1\uac00\uc9c0\ub294 \ubb34\uc5c7\uc778\uac00\uc694?"
        )
    prompt = (
        "Generate 5 probation-review questions in Korean from this note. "
        "Focus on concrete behavior, evidence, and improvement actions.\n\n"
        f"Note:\n{note}"
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
        f"?key={api_key}"
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()
    data = response.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


DECISION_LABELS = {
    "SUPER_PASS": "\ud569\uaca9 (\ud575\uc2ec\uc778\uc7ac)",
    "PASS": "\ud569\uaca9 (\uc6b0\uc218\uc778\uc7ac)",
    "EXTENSION": "\uc218\uc2b5 \uc5f0\uc7a5",
    "FAIL": "\ubd88\ud569\uaca9",
    "IN_PROGRESS": "\uc9c4\ud589 \uc911",
}

COMMON_STYLE = """
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>\uc218\uc2b5\ud3c9\uac00 \uc2dc\uc2a4\ud15c</title>
<style>
  body { font-family: 'Segoe UI', 'Malgun Gothic', sans-serif; max-width: 960px; margin: 0 auto; padding: 16px; color: #222; }
  nav { background: #2c3e50; padding: 10px 16px; border-radius: 6px; margin-bottom: 20px; }
  nav a { color: #ecf0f1; text-decoration: none; margin-right: 16px; font-size: 14px; }
  nav a:hover { text-decoration: underline; }
  nav .right { float: right; }
  h2 { border-bottom: 2px solid #333; padding-bottom: 8px; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #ccc; padding: 8px; text-align: left; }
  th { background: #f5f5f5; }
  button { padding: 8px 20px; cursor: pointer; background: #2c3e50; color: #fff; border: none; border-radius: 4px; }
  button:hover { background: #1a252f; }
  .card { border: 1px solid #ddd; padding: 14px; margin: 10px 0; border-radius: 6px; background: #fafafa; }
  a { color: #1a73e8; }
  textarea { width: 100%; box-sizing: border-box; }
  fieldset { margin: 10px 0; padding: 12px; }
  .grade-desc { font-size: 12px; color: #555; margin-left: 4px; }
  select { padding: 4px 8px; }
  input[type="file"] { margin: 4px 0; }
</style>
</head>
<body>
"""


FOOTER = "\n</body></html>"

NAV_ADMIN = (
    '<nav>'
    '<a href="/admin">\ub300\uc2dc\ubcf4\ub4dc</a>'
    '<a href="/admin/users">\uc0ac\uc6a9\uc790 \uad00\ub9ac</a>'
    '<a href="/admin/cycles">\uc0ac\uc774\ud074 \uad00\ub9ac</a>'
    '<a href="/admin/peers">\ub3d9\ub8cc\ud3c9\uac00\uc790 \uad00\ub9ac</a>'
    '<a class="right" href="/logout">\ub85c\uadf8\uc544\uc6c3</a>'
    '</nav>'
)
NAV_TARGET = (
    '<nav>'
    '<a href="/target">\ub300\uc2dc\ubcf4\ub4dc</a>'
    '<a class="right" href="/logout">\ub85c\uadf8\uc544\uc6c3</a>'
    '</nav>'
)
NAV_EVALUATOR = (
    '<nav>'
    '<a href="/evaluator">\ub300\uc2dc\ubcf4\ub4dc</a>'
    '<a class="right" href="/logout">\ub85c\uadf8\uc544\uc6c3</a>'
    '</nav>'
)


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
        error = "\ub85c\uadf8\uc778 \uc815\ubcf4\uac00 \uc62c\ubc14\ub974\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4."
    return render_template_string(
        COMMON_STYLE + """
        <h2>\uc218\uc2b5\ud3c9\uac00 \uc2dc\uc2a4\ud15c \ub85c\uadf8\uc778</h2>
        <p style="color:#666;">\uae30\ubcf8 \uacc4\uc815: admin@company.local / admin1234</p>
        {% if error %}<p style="color:#c0392b;">{{error}}</p>{% endif %}
        <form method="post">
          <label>Email</label><br/>
          <input type="email" name="email" required/><br/><br/>
          <label>\ube44\ubc00\ubc88\ud638</label><br/>
          <input type="password" name="password" required/><br/><br/>
          <button type="submit">\ub85c\uadf8\uc778</button>
        </form>
        """ + FOOTER,
        error=error,
    )


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


@app.route("/admin", methods=["GET", "POST"])
@require_role("admin")
def admin_dashboard():
    db = get_db()
    if request.method == "POST":
        if request.form.get("form_type") == "policy":
            peer_visibility = request.form.get("peer_visibility", "evaluator_only")
            if peer_visibility not in PEER_VISIBILITY_OPTIONS:
                peer_visibility = "evaluator_only"
            db.execute(
                """
                INSERT INTO app_settings(key, value) VALUES ('peer_visibility', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (peer_visibility,),
            )
            db.commit()
            return redirect(url_for("admin_dashboard"))

        target_user_id = request.form.get("target_user_id")
        cycle_id = request.form.get("cycle_id")
        evaluator_ids = request.form.getlist("evaluator_ids")[:3]
        cursor = db.execute(
            "INSERT INTO evaluatees(user_id,cycle_id,peer_survey_token,created_at) VALUES (?,?,?,?) RETURNING id",
            (target_user_id, cycle_id, secrets.token_hex(8), now()),
        )
        inserted = cursor.fetchone()
        evaluatee_id = inserted["id"] if hasattr(inserted, "keys") else inserted[0]
        for evaluator_id in evaluator_ids:
            db.execute(
                "INSERT INTO evaluator_assignments(evaluatee_id,evaluator_user_id) VALUES (?,?) ON CONFLICT(evaluatee_id,evaluator_user_id) DO NOTHING",
                (evaluatee_id, evaluator_id),
            )
        db.commit()
        return redirect(url_for("admin_dashboard"))

    targets = db.execute("SELECT * FROM users WHERE role='target' ORDER BY id").fetchall()
    evaluators = db.execute("SELECT * FROM users WHERE role='evaluator' ORDER BY id").fetchall()
    cycles = db.execute("SELECT * FROM evaluation_cycles ORDER BY id DESC").fetchall()
    peer_visibility = get_peer_visibility(db)
    evaluatees = db.execute(
        """
        SELECT e.id, u.name AS target_name, c.name AS cycle_name, e.peer_survey_token, e.presentation_filename, ar.decision
        FROM evaluatees e
        JOIN users u ON u.id = e.user_id
        JOIN evaluation_cycles c ON c.id = e.cycle_id
        LEFT JOIN aggregated_results ar ON ar.evaluatee_id = e.id
        ORDER BY e.id DESC
        """
    ).fetchall()
    return render_template_string(
        COMMON_STYLE + NAV_ADMIN + """
        <h2>\uad00\ub9ac\uc790 \ub300\uc2dc\ubcf4\ub4dc</h2>
        <h3>\ub3d9\ub8cc\ud3c9\uac00 \uacf5\uac1c \uc815\ucc45</h3>
        <form method="post">
          <input type="hidden" name="form_type" value="policy"/>
          <select name="peer_visibility">
            {% for key, label in peer_visibility_options.items() %}
              <option value="{{key}}" {% if key == peer_visibility %}selected{% endif %}>{{label}}</option>
            {% endfor %}
          </select>
          <button type="submit">\uc815\ucc45 \uc800\uc7a5</button>
        </form>
        <h3>\ud3c9\uac00 \ub300\uc0c1\uc790 \uc0dd\uc131</h3>
        <form method="post">
          <label>\ub300\uc0c1\uc790</label>
          <select name="target_user_id">{% for t in targets %}<option value="{{t.id}}">{{t.name}}</option>{% endfor %}</select>
          <label>\ud3c9\uac00 \uc0ac\uc774\ud074</label>
          <select name="cycle_id">{% for c in cycles %}<option value="{{c.id}}">{{c.name}}</option>{% endfor %}</select>
          <fieldset>
            <legend>\ud3c9\uac00\uc790 (\ucd5c\ub300 3\uba85)</legend>
            {% for e in evaluators %}
              <label><input type="checkbox" name="evaluator_ids" value="{{e.id}}"> {{e.name}}</label><br/>
            {% endfor %}
          </fieldset>
          <button type="submit">\uc0dd\uc131</button>
        </form>
        <h3>\ub300\uc0c1\uc790 \ubaa9\ub85d</h3>
        <table>
          <tr><th>ID</th><th>\ub300\uc0c1\uc790</th><th>\uc0ac\uc774\ud074</th><th>\ub3d9\ub8cc \uc124\ubb38 \ub9c1\ud06c</th><th>PT \ud30c\uc77c</th><th>\ud310\uc815</th><th>\uc561\uc158</th></tr>
          {% for e in evaluatees %}
          <tr>
            <td>{{e.id}}</td><td>{{e.target_name}}</td><td>{{e.cycle_name}}</td><td>/peer-survey/{{e.peer_survey_token}}</td>
            <td>{{e.presentation_filename or '-'}}</td><td>{{decision_labels.get(e.decision or 'IN_PROGRESS', e.decision or '\uc9c4\ud589 \uc911')}}</td>
            <td><a href="{{url_for('aggregate_result', evaluatee_id=e.id)}}">\ucde8\ud569</a> | <a href="{{url_for('deliver_feedback', evaluatee_id=e.id)}}">\uc804\ub2ec</a></td>
          </tr>
          {% endfor %}
        </table>
        """ + FOOTER,
        targets=targets,
        evaluators=evaluators,
        cycles=cycles,
        evaluatees=evaluatees,
        peer_visibility=peer_visibility,
        peer_visibility_options=PEER_VISIBILITY_OPTIONS,
        decision_labels=DECISION_LABELS,
    )


@app.route("/target", methods=["GET", "POST"])
@require_role("target")
def target_dashboard():
    db = get_db()
    user = current_user()
    notice = request.args.get("notice", "")
    evaluatee = db.execute("SELECT * FROM evaluatees WHERE user_id=? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
    if not evaluatee:
        return "\ubc30\uc815\ub41c \ud3c9\uac00\uac00 \uc5c6\uc2b5\ub2c8\ub2e4."
    items = db.execute("SELECT * FROM assessment_items ORDER BY id").fetchall()
    existing = db.execute("SELECT * FROM self_assessments WHERE evaluatee_id=?", (evaluatee["id"],)).fetchall()
    existing_by_item = {row["item_id"]: row for row in existing}
    if request.method == "POST":
        file = request.files.get("presentation")
        if file and file.filename:
            UPLOAD_DIR.mkdir(exist_ok=True)
            safe_name = secure_filename(file.filename)
            if not safe_name:
                safe_name = "presentation.pdf"
            filename = f"{evaluatee['id']}_{int(datetime.now().timestamp())}_{safe_name}"
            try:
                content = file.read()
                if OBJECT_STORAGE_ENABLED:
                    object_key = f"presentations/{filename}"
                    uploaded = upload_to_object_storage(object_key, content, file.mimetype)
                    if uploaded:
                        db.execute(
                            "UPDATE evaluatees SET presentation_filename=?, presentation_file_id=?, presentation_storage='s3' WHERE id=?",
                            (filename, object_key, evaluatee["id"]),
                        )
                        notice = "PT \ud30c\uc77c\uc774 \ud074\ub77c\uc6b0\ub4dc \uc800\uc7a5\uc18c\uc5d0 \uc800\uc7a5\ub418\uc5c8\uc2b5\ub2c8\ub2e4."
                    else:
                        with open(UPLOAD_DIR / filename, "wb") as fp:
                            fp.write(content)
                        db.execute(
                            "UPDATE evaluatees SET presentation_filename=?, presentation_file_id=NULL, presentation_storage='local' WHERE id=?",
                            (filename, evaluatee["id"]),
                        )
                        notice = "\ud074\ub77c\uc6b0\ub4dc \uc5f0\ub3d9 \uc2e4\ud328\ub85c \ub85c\uceec\uc5d0 \uc800\uc7a5\ub418\uc5c8\uc2b5\ub2c8\ub2e4."
                else:
                    with open(UPLOAD_DIR / filename, "wb") as fp:
                        fp.write(content)
                    db.execute(
                        "UPDATE evaluatees SET presentation_filename=?, presentation_file_id=NULL, presentation_storage='local' WHERE id=?",
                        (filename, evaluatee["id"]),
                    )
                    notice = "PT \ud30c\uc77c\uc774 \uc815\uc0c1 \uc800\uc7a5\ub418\uc5c8\uc2b5\ub2c8\ub2e4."
            except OSError:
                notice = "PT \ud30c\uc77c \uc800\uc7a5\uc5d0 \uc2e4\ud328\ud588\uc2b5\ub2c8\ub2e4. \ud30c\uc77c\uba85/\uc6a9\ub7c9\uc744 \ud655\uc778\ud574 \uc8fc\uc138\uc694."
        keep_text = request.form.get("keep_text", "")
        problem_text = request.form.get("problem_text", "")
        try_text = request.form.get("try_text", "")
        for item in items:
            grade = request.form.get(f"grade_{item['id']}", "B")
            db.execute(
                """
                INSERT INTO self_assessments(evaluatee_id,item_id,grade,keep_text,problem_text,try_text,updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(evaluatee_id,item_id) DO UPDATE SET
                    grade=excluded.grade,
                    keep_text=excluded.keep_text,
                    problem_text=excluded.problem_text,
                    try_text=excluded.try_text,
                    updated_at=excluded.updated_at
                """,
                (evaluatee["id"], item["id"], grade, keep_text, problem_text, try_text, now()),
            )
        db.execute("UPDATE evaluatees SET self_submitted_at=? WHERE id=?", (now(), evaluatee["id"]))
        db.commit()
        return redirect(url_for("target_dashboard", notice=notice))
    result = db.execute("SELECT * FROM aggregated_results WHERE evaluatee_id=?", (evaluatee["id"],)).fetchone()
    return render_template_string(
        COMMON_STYLE + NAV_TARGET + """
        <h2>\ud3c9\uac00 \ub300\uc0c1\uc790 \ud654\uba74</h2>
        {% if notice %}<p style="color:#1e8449;"><b>{{notice}}</b></p>{% endif %}
        <h3>PT \uc5c5\ub85c\ub4dc + \uc790\uac00\ud3c9\uac00</h3>
        <form method="post" enctype="multipart/form-data">
          <label><b>PT \ubc1c\ud45c \uc790\ub8cc</b></label><br/>
          <input type="file" name="presentation"/><br/><br/>
          <p>\ud604\uc7ac \uc800\uc7a5 \ud30c\uc77c: {% if evaluatee.presentation_filename %}<a href="{{url_for('presentation_file', evaluatee_id=evaluatee.id)}}">{{evaluatee.presentation_filename}}</a>{% else %}-{% endif %}</p>
          {% for item in items %}
            <div class="card">
              <b>{{item.title}}</b><br/><small>{{item.prompt}}</small><br/>
              {% set current = existing_by_item[item.id].grade if item.id in existing_by_item else 'B' %}
              {% for g in ['S','A','B','C'] %}
                <label><input type="radio" name="grade_{{item.id}}" value="{{g}}" {% if current==g %}checked{% endif %}>{{g}}
                  <span class="grade-desc">({% if g=='S' %}{{item.grade_s}}{% elif g=='A' %}{{item.grade_a}}{% elif g=='B' %}{{item.grade_b}}{% else %}{{item.grade_c}}{% endif %})</span>
                </label><br/>
              {% endfor %}
            </div>
          {% endfor %}
          <label>Keep(\uc798\ud55c \uc810)</label><br/><textarea name="keep_text" rows="3">{{existing[0].keep_text if existing else ''}}</textarea><br/>
          <label>Problem(\uc544\uc26c\uc6b4 \uc810)</label><br/><textarea name="problem_text" rows="3">{{existing[0].problem_text if existing else ''}}</textarea><br/>
          <label>Try(\uac1c\uc120 \uc2dc\ub3c4)</label><br/><textarea name="try_text" rows="3">{{existing[0].try_text if existing else ''}}</textarea><br/>
          <button type="submit">\uc800\uc7a5</button>
        </form>
        <h3>\uacb0\uacfc</h3>
        {% if result %}
          <p><b>\ud310\uc815:</b> {{decision_labels.get(result.decision, result.decision)}}</p>
          <p><b>\uc694\uc57d:</b> {{result.summary}}</p>
          <p><b>\ud53c\ub4dc\ubc31:</b> {{result.admin_feedback or '-'}}</p>
        {% else %}
          <p>\uc544\uc9c1 \uacb0\uacfc\uac00 \uc804\ub2ec\ub418\uc9c0 \uc54a\uc558\uc2b5\ub2c8\ub2e4.</p>
        {% endif %}
        """ + FOOTER,
        items=items,
        existing=existing,
        existing_by_item=existing_by_item,
        result=result,
        evaluatee=evaluatee,
        notice=notice,
        decision_labels=DECISION_LABELS,
    )


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
        row = get_db().execute(
            "SELECT 1 FROM evaluator_assignments WHERE evaluatee_id=? AND evaluator_user_id=?",
            (evaluatee["id"], user["id"]),
        ).fetchone()
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
        return Response(
            content,
            mimetype=mime,
            headers={"Content-Disposition": f'inline; filename="{evaluatee["presentation_filename"]}"'},
        )
    if storage == "gdrive" and evaluatee["presentation_file_id"]:
        name, mime, content = download_from_google_drive(evaluatee["presentation_file_id"])
        if content is None:
            abort(404)
        return Response(
            content,
            mimetype=mime,
            headers={"Content-Disposition": f'inline; filename="{name or evaluatee["presentation_filename"]}"'},
        )
    return send_from_directory(UPLOAD_DIR, evaluatee["presentation_filename"], as_attachment=False)


@app.route("/evaluator", methods=["GET", "POST"])
@require_role("evaluator")
def evaluator_dashboard():
    db = get_db()
    user = current_user()
    assignments = db.execute(
        """
        SELECT e.id AS evaluatee_id, u.name AS target_name, e.presentation_filename
        FROM evaluator_assignments ea
        JOIN evaluatees e ON e.id = ea.evaluatee_id
        JOIN users u ON u.id = e.user_id
        WHERE ea.evaluator_user_id = ?
        ORDER BY e.id DESC
        """,
        (user["id"],),
    ).fetchall()
    selected = request.args.get("evaluatee_id")
    if request.method == "POST":
        evaluatee_id = request.form.get("evaluatee_id")
        items = db.execute("SELECT * FROM assessment_items ORDER BY id").fetchall()
        for item in items:
            db.execute(
                """
                INSERT INTO leader_assessments(evaluatee_id,evaluator_user_id,item_id,grade,feedback_text,presentation_note,qa_note,updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(evaluatee_id,evaluator_user_id,item_id) DO UPDATE SET
                    grade=excluded.grade,
                    feedback_text=excluded.feedback_text,
                    presentation_note=excluded.presentation_note,
                    qa_note=excluded.qa_note,
                    updated_at=excluded.updated_at
                """,
                (
                    evaluatee_id,
                    user["id"],
                    item["id"],
                    request.form.get(f"grade_{item['id']}", "B"),
                    request.form.get("feedback_text", ""),
                    request.form.get("presentation_note", ""),
                    request.form.get("qa_note", ""),
                    now(),
                ),
            )
        db.commit()
        return redirect(url_for("evaluator_dashboard", evaluatee_id=evaluatee_id))
    detail = None
    self_data = []
    items = []
    leader_data = {}
    peer_summary = ""
    ai_questions = ""
    if selected:
        assigned = db.execute(
            "SELECT 1 FROM evaluator_assignments WHERE evaluatee_id=? AND evaluator_user_id=?",
            (selected, user["id"]),
        ).fetchone()
        if not assigned:
            abort(403)
        detail = db.execute(
            "SELECT e.id, e.presentation_filename, u.name AS target_name FROM evaluatees e JOIN users u ON u.id = e.user_id WHERE e.id = ?",
            (selected,),
        ).fetchone()
        if not detail:
            abort(404)
        self_data = db.execute(
            """
            SELECT ai.title, sa.grade, sa.keep_text, sa.problem_text, sa.try_text
            FROM self_assessments sa
            JOIN assessment_items ai ON ai.id = sa.item_id
            WHERE sa.evaluatee_id = ?
            ORDER BY ai.id
            """,
            (selected,),
        ).fetchall()
        items = db.execute("SELECT * FROM assessment_items ORDER BY id").fetchall()
        existing = db.execute(
            "SELECT * FROM leader_assessments WHERE evaluatee_id=? AND evaluator_user_id=?",
            (selected, user["id"]),
        ).fetchall()
        leader_data = {row["item_id"]: row for row in existing}
        peer_visibility = get_peer_visibility(db)
        if peer_visibility in ("evaluator_only", "admin_and_evaluator"):
            peer_summary = summarize_peer_comments(db, selected)
        else:
            peer_summary = "\uad00\ub9ac\uc790 \uc804\uc6a9 \uc815\ucc45\uc73c\ub85c \ud3c9\uac00\uc790\uc5d0\uac8c \ube44\uacf5\uac1c\uc785\ub2c8\ub2e4."
        ai_row = db.execute(
            "SELECT suggested_questions FROM ai_question_logs WHERE evaluatee_id=? AND evaluator_user_id=? ORDER BY id DESC LIMIT 1",
            (selected, user["id"]),
        ).fetchone()
        if ai_row:
            ai_questions = ai_row["suggested_questions"]
    return render_template_string(
        COMMON_STYLE + NAV_EVALUATOR + """
        <h2>\ud3c9\uac00\uc790 \ud654\uba74</h2>
        <h3>\ubc30\uc815 \ub300\uc0c1\uc790</h3>
        <ul>{% for a in assignments %}<li><a href="{{url_for('evaluator_dashboard', evaluatee_id=a.evaluatee_id)}}">{{a.target_name}}</a></li>{% endfor %}</ul>
        {% if detail %}
          <h3>{{detail.target_name}}</h3>
          <p>PT \ud30c\uc77c: {% if detail.presentation_filename %}<a href="{{url_for('presentation_file', evaluatee_id=detail.id)}}">{{detail.presentation_filename}}</a>{% else %}\ubbf8\uc5c5\ub85c\ub4dc{% endif %}</p>
          <p>\ub3d9\ub8cc\ud3c9\uac00 \ucde8\ud569: {{peer_summary}}</p>
          <h4>\uc790\uac00\ud3c9\uac00 \uc870\ud68c</h4>
          {% for s in self_data %}
            <div class="card">
              <b>{{s.title}} - {{s.grade}}</b><br/>Keep: {{s.keep_text or '-'}}<br/>Problem: {{s.problem_text or '-'}}<br/>Try: {{s.try_text or '-'}}
            </div>
          {% endfor %}
          <h4>\ub9ac\ub354 \ud3c9\uac00</h4>
          <form method="post">
            <input type="hidden" name="evaluatee_id" value="{{detail.id}}"/>
            {% for item in items %}
              {% set current = leader_data[item.id].grade if item.id in leader_data else 'B' %}
              <div class="card">
                <b>{{item.title}}</b><br/><small>{{item.prompt}}</small><br/>
                {% for g in ['S','A','B','C'] %}
                  <label><input type="radio" name="grade_{{item.id}}" value="{{g}}" {% if g==current %}checked{% endif %}>{{g}}
                    <span class="grade-desc">({% if g=='S' %}{{item.grade_s}}{% elif g=='A' %}{{item.grade_a}}{% elif g=='B' %}{{item.grade_b}}{% else %}{{item.grade_c}}{% endif %})</span>
                  </label><br/>
                {% endfor %}
              </div>
            {% endfor %}
            <label>PT \uba54\ubaa8</label><br/><textarea name="presentation_note" rows="3">{{ leader_data.values()|list|first.presentation_note if leader_data else '' }}</textarea><br/>
            <label>Q&A \uba54\ubaa8</label><br/><textarea name="qa_note" rows="3">{{ leader_data.values()|list|first.qa_note if leader_data else '' }}</textarea><br/>
            <label>\ub300\uc0c1\uc790 \uc804\ub2ec \ud53c\ub4dc\ubc31</label><br/><textarea name="feedback_text" rows="4">{{ leader_data.values()|list|first.feedback_text if leader_data else '' }}</textarea><br/>
            <button type="submit">\uc800\uc7a5</button>
          </form>
          <h4>AI \uc9c8\ubb38 \uc810\uac80</h4>
          <form method="post" action="{{url_for('ai_questions', evaluatee_id=detail.id)}}">
            <textarea name="source_note" rows="4" placeholder="\ubc1c\ud45c/\uc9c8\uc758\uc751\ub2f5 \uba54\ubaa8\ub97c \uc785\ub825\ud558\uba74 \uc9c8\ubb38\uc548\uc744 \uc0dd\uc131\ud569\ub2c8\ub2e4."></textarea><br/>
            <button type="submit">\uc9c8\ubb38 \uc0dd\uc131</button>
          </form>
          {% if ai_questions %}<pre>{{ai_questions}}</pre>{% endif %}
        {% endif %}
        """ + FOOTER,
        assignments=assignments,
        detail=detail,
        self_data=self_data,
        items=items,
        leader_data=leader_data,
        peer_summary=peer_summary,
        ai_questions=ai_questions,
    )


@app.route("/evaluator/<int:evaluatee_id>/ai-questions", methods=["POST"])
@require_role("evaluator")
def ai_questions(evaluatee_id):
    db = get_db()
    user = current_user()
    source_note = request.form.get("source_note", "").strip()
    if not source_note:
        return redirect(url_for("evaluator_dashboard", evaluatee_id=evaluatee_id))
    suggested = build_ai_questions(source_note)
    db.execute(
        "INSERT INTO ai_question_logs(evaluatee_id,evaluator_user_id,source_note,suggested_questions,created_at) VALUES (?,?,?,?,?)",
        (evaluatee_id, user["id"], source_note, suggested, now()),
    )
    db.commit()
    return redirect(url_for("evaluator_dashboard", evaluatee_id=evaluatee_id))


@app.route("/peer-survey/<token>", methods=["GET", "POST"])
def peer_survey(token):
    db = get_db()
    evaluatee = db.execute(
        "SELECT e.id, u.name AS target_name FROM evaluatees e JOIN users u ON u.id=e.user_id WHERE e.peer_survey_token=?",
        (token,),
    ).fetchone()
    peers = db.execute("SELECT id, name FROM peer_reviewers ORDER BY id").fetchall()
    if not evaluatee:
        abort(404)
    if request.method == "POST":
        comment = request.form.get("peer_comment", "").strip()
        peer_id = request.form.get("peer_id", "").strip()
        peer_name = request.form.get("peer_name", "").strip()
        if peer_id:
            peer_row = db.execute("SELECT name FROM peer_reviewers WHERE id=?", (peer_id,)).fetchone()
            if peer_row:
                peer_name = peer_row["name"]
        if comment:
            db.execute(
                "INSERT INTO peer_surveys(evaluatee_id,peer_name,peer_comment,created_at) VALUES (?,?,?,?)",
                (evaluatee["id"], peer_name, comment, now()),
            )
            db.commit()
            return "\uc81c\ucd9c\ub418\uc5c8\uc2b5\ub2c8\ub2e4."
    return render_template_string(
        COMMON_STYLE + """
        <h2>\ub3d9\ub8cc \ud3c9\uac00 \uc124\ubb38</h2>
        <p>\ub300\uc0c1\uc790: {{evaluatee.target_name}}</p>
        <form method="post">
          <label>\ub3d9\ub8cc\ud3c9\uac00\uc790 \uc120\ud0dd</label><br/>
          <select name="peer_id">
            <option value="">-- \uc120\ud0dd --</option>
            {% for p in peers %}
              <option value="{{p.id}}">{{p.name}}</option>
            {% endfor %}
          </select><br/><br/>
          <label>\uc9c1\uc811 \uc785\ub825(\uc120\ud0dd)</label><br/><input type="text" name="peer_name"/><br/>
          <label>\uc758\uacac</label><br/><textarea name="peer_comment" rows="6" required></textarea><br/>
          <button type="submit">\uc81c\ucd9c</button>
        </form>
        """ + FOOTER,
        evaluatee=evaluatee,
        peers=peers,
    )


@app.route("/admin/aggregate/<int:evaluatee_id>")
@require_role("admin")
def aggregate_result(evaluatee_id):
    db = get_db()
    item_rows = db.execute("SELECT id, title FROM assessment_items ORDER BY id").fetchall()
    item_grades = get_item_grades_for_evaluatee(db, evaluatee_id)
    decision = decide_result(item_grades)
    labels = [f"{item['title']}={item_grades.get(item['id'], 'N/A')}" for item in item_rows]
    peer_visibility = get_peer_visibility(db)
    if peer_visibility in ("admin_only", "admin_and_evaluator"):
        peer_text = summarize_peer_comments(db, evaluatee_id)
    else:
        peer_text = "\ub3d9\ub8cc\ud3c9\uac00 \uc694\uc57d\uc740 \uad00\ub9ac\uc790 \ube44\uacf5\uac1c \uc815\ucc45\uc785\ub2c8\ub2e4."
    summary = " / ".join(labels) + " / " + peer_text
    db.execute(
        """
        INSERT INTO aggregated_results(evaluatee_id,decision,summary,updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(evaluatee_id) DO UPDATE SET
            decision=excluded.decision,
            summary=excluded.summary,
            updated_at=excluded.updated_at
        """,
        (evaluatee_id, decision, summary, now()),
    )
    db.commit()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/deliver/<int:evaluatee_id>", methods=["GET", "POST"])
@require_role("admin")
def deliver_feedback(evaluatee_id):
    db = get_db()
    result = db.execute("SELECT * FROM aggregated_results WHERE evaluatee_id=?", (evaluatee_id,)).fetchone()
    if not result:
        return "\uba3c\uc800 \ucde8\ud569\uc744 \uc2e4\ud589\ud574 \uc8fc\uc138\uc694."
    if request.method == "POST":
        admin_feedback = request.form.get("admin_feedback", "")
        db.execute(
            "UPDATE aggregated_results SET admin_feedback=?, delivered_at=?, updated_at=? WHERE evaluatee_id=?",
            (admin_feedback, now(), now(), evaluatee_id),
        )
        db.commit()
        return redirect(url_for("admin_dashboard"))
    return render_template_string(
        COMMON_STYLE + NAV_ADMIN + """
        <h2>\uacb0\uacfc \uc804\ub2ec</h2>
        <p>\ud310\uc815: {{decision_labels.get(result.decision, result.decision)}}</p>
        <p>\uc694\uc57d: {{result.summary}}</p>
        <form method="post">
          <label>\uad00\ub9ac\uc790 \ud53c\ub4dc\ubc31</label><br/>
          <textarea name="admin_feedback" rows="6">{{result.admin_feedback or ''}}</textarea><br/>
          <button type="submit">\uc804\ub2ec</button>
        </form>
        """ + FOOTER,
        result=result,
        decision_labels=DECISION_LABELS,
    )


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
            if name and email and role in ("admin", "target", "evaluator"):
                if not password:
                    password = "target1234" if role == "target" else ("leader1234" if role == "evaluator" else "admin1234")
                db.execute(
                    "INSERT INTO users(name, email, role, password_hash) VALUES (?, ?, ?, ?)",
                    (name, email, role, generate_password_hash(password)),
                )
                db.commit()
        elif action == "reset_password":
            user_id = request.form.get("user_id")
            new_password = request.form.get("new_password", "").strip()
            if user_id and new_password:
                db.execute(
                    "UPDATE users SET password_hash=? WHERE id=?",
                    (generate_password_hash(new_password), user_id),
                )
                db.commit()
        elif action == "delete":
            user_id = request.form.get("user_id")
            if user_id:
                db.execute("DELETE FROM users WHERE id=?", (user_id,))
                db.commit()
        return redirect(url_for("manage_users"))
    users = db.execute("SELECT * FROM users ORDER BY role, id").fetchall()
    return render_template_string(
        COMMON_STYLE + NAV_ADMIN + """
        <h2>\uc0ac\uc6a9\uc790 \uad00\ub9ac</h2>
        <h3>\uc0ac\uc6a9\uc790 \ucd94\uac00</h3>
        <form method="post">
          <input type="hidden" name="action" value="add"/>
          <label>\uc774\ub984</label> <input type="text" name="name" required/>
          <label>Email</label> <input type="email" name="email" required/>
          <label>\uc5ed\ud560</label>
          <select name="role">
            <option value="target">\ud3c9\uac00\ub300\uc0c1\uc790</option>
            <option value="evaluator">\ud3c9\uac00\uc790</option>
            <option value="admin">\uad00\ub9ac\uc790</option>
          </select>
          <label>\ucd08\uae30 \ube44\ubc00\ubc88\ud638</label> <input type="text" name="password" placeholder="\ubbf8\uc785\ub825 \uc2dc \uc5ed\ud560\ubcc4 \uae30\ubcf8\uac12"/>
          <button type="submit">\ucd94\uac00</button>
        </form>
        <h3>\uc0ac\uc6a9\uc790 \ubaa9\ub85d</h3>
        <table>
          <tr><th>ID</th><th>\uc774\ub984</th><th>Email</th><th>\uc5ed\ud560</th><th>\ube44\ubc00\ubc88\ud638 \uc7ac\uc124\uc815</th><th>\uc561\uc158</th></tr>
          {% for u in users %}
          <tr>
            <td>{{u.id}}</td><td>{{u.name}}</td><td>{{u.email}}</td>
            <td>{% if u.role=='admin' %}\uad00\ub9ac\uc790{% elif u.role=='target' %}\ud3c9\uac00\ub300\uc0c1\uc790{% else %}\ud3c9\uac00\uc790{% endif %}</td>
            <td>
              <form method="post" style="display:inline">
                <input type="hidden" name="action" value="reset_password"/>
                <input type="hidden" name="user_id" value="{{u.id}}"/>
                <input type="text" name="new_password" placeholder="\uc0c8 \ube44\ubc00\ubc88\ud638" required/>
                <button type="submit">\ubcc0\uacbd</button>
              </form>
            </td>
            <td>
              <form method="post" style="display:inline">
                <input type="hidden" name="action" value="delete"/>
                <input type="hidden" name="user_id" value="{{u.id}}"/>
                <button type="submit" onclick="return confirm('\uc0ad\uc81c\ud558\uc2dc\uaca0\uc2b5\ub2c8\uae4c?')">\uc0ad\uc81c</button>
              </form>
            </td>
          </tr>
          {% endfor %}
        </table>
        """ + FOOTER,
        users=users,
    )


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
                db.execute(
                    "INSERT INTO evaluation_cycles(name, start_date, end_date) VALUES (?, ?, ?)",
                    (name, start_date, end_date),
                )
                db.commit()
        elif action == "delete":
            cycle_id = request.form.get("cycle_id")
            if cycle_id:
                db.execute("DELETE FROM evaluation_cycles WHERE id=?", (cycle_id,))
                db.commit()
        return redirect(url_for("manage_cycles"))
    cycles = db.execute("SELECT * FROM evaluation_cycles ORDER BY id DESC").fetchall()
    return render_template_string(
        COMMON_STYLE + NAV_ADMIN + """
        <h2>\ud3c9\uac00 \uc0ac\uc774\ud074 \uad00\ub9ac</h2>
        <h3>\uc0ac\uc774\ud074 \ucd94\uac00</h3>
        <form method="post">
          <input type="hidden" name="action" value="add"/>
          <label>\uc0ac\uc774\ud074\uba85</label> <input type="text" name="name" required/>
          <label>\uc2dc\uc791\uc77c</label> <input type="date" name="start_date"/>
          <label>\uc885\ub8cc\uc77c</label> <input type="date" name="end_date"/>
          <button type="submit">\ucd94\uac00</button>
        </form>
        <h3>\uc0ac\uc774\ud074 \ubaa9\ub85d</h3>
        <table>
          <tr><th>ID</th><th>\uc0ac\uc774\ud074\uba85</th><th>\uc2dc\uc791\uc77c</th><th>\uc885\ub8cc\uc77c</th><th>\uc0c1\ud0dc</th><th>\uc561\uc158</th></tr>
          {% for c in cycles %}
          <tr>
            <td>{{c.id}}</td><td>{{c.name}}</td><td>{{c.start_date or '-'}}</td><td>{{c.end_date or '-'}}</td><td>{{c.status}}</td>
            <td>
              <form method="post" style="display:inline">
                <input type="hidden" name="action" value="delete"/>
                <input type="hidden" name="cycle_id" value="{{c.id}}"/>
                <button type="submit" onclick="return confirm('\uc0ad\uc81c\ud558\uc2dc\uaca0\uc2b5\ub2c8\uae4c?')">\uc0ad\uc81c</button>
              </form>
            </td>
          </tr>
          {% endfor %}
        </table>
        """ + FOOTER,
        cycles=cycles,
    )


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
                db.commit()
        elif action == "delete":
            peer_id = request.form.get("peer_id")
            if peer_id:
                db.execute("DELETE FROM peer_reviewers WHERE id=?", (peer_id,))
                db.commit()
        return redirect(url_for("manage_peers"))
    peers = db.execute("SELECT * FROM peer_reviewers ORDER BY id").fetchall()
    return render_template_string(
        COMMON_STYLE + NAV_ADMIN + """
        <h2>\ub3d9\ub8cc\ud3c9\uac00\uc790 \uad00\ub9ac</h2>
        <form method="post">
          <input type="hidden" name="action" value="add"/>
          <label>\uc774\ub984</label> <input type="text" name="name" required/>
          <button type="submit">\ucd94\uac00</button>
        </form>
        <h3>\ub3d9\ub8cc\ud3c9\uac00\uc790 \ubaa9\ub85d</h3>
        <table>
          <tr><th>ID</th><th>\uc774\ub984</th><th>\uc561\uc158</th></tr>
          {% for p in peers %}
          <tr>
            <td>{{p.id}}</td><td>{{p.name}}</td>
            <td>
              <form method="post" style="display:inline">
                <input type="hidden" name="action" value="delete"/>
                <input type="hidden" name="peer_id" value="{{p.id}}"/>
                <button type="submit" onclick="return confirm('\uc0ad\uc81c\ud558\uc2dc\uaca0\uc2b5\ub2c8\uae4c?')">\uc0ad\uc81c</button>
              </form>
            </td>
          </tr>
          {% endfor %}
        </table>
        """ + FOOTER,
        peers=peers,
    )


@app.route("/health")
def health():
    return {"status": "ok", "time": now()}


if __name__ == "__main__":
    ensure_db_initialized()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
