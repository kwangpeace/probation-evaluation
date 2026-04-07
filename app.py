# -*- coding: cp949 -*-
import os
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, abort, g, redirect, render_template_string, request, send_from_directory, session, url_for
from werkzeug.utils import secure_filename
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

GRADE_TO_SCORE = {"S": 4, "A": 3, "B": 2, "C": 1}
SCORE_TO_GRADE = {4: "S", 3: "A", 2: "B", 1: "C"}
PEER_VISIBILITY_OPTIONS = {
    "evaluator_only": "????? ????",
    "admin_only": "??????? ????",
    "admin_and_evaluator": "??????+???? ????",
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
        """
    db.executescript(schema_script)

    if scalar_count(db, "users") == 0:
        db.executemany(
            "INSERT INTO users(name, email, role) VALUES (?, ?, ?)",
            [
                ("HR Admin", "admin@company.local", "admin"),
                ("Target Kim", "target1@company.local", "target"),
                ("Target Park", "target2@company.local", "target"),
                ("Leader Lee", "leader1@company.local", "evaluator"),
                ("Leader Choi", "leader2@company.local", "evaluator"),
                ("Leader Jung", "leader3@company.local", "evaluator"),
            ],
        )
    if scalar_count(db, "evaluation_cycles") == 0:
        db.execute(
            "INSERT INTO evaluation_cycles(name, start_date, end_date) VALUES (?, ?, ?)",
            ("2026 Q1 Probation Review", "2026-01-01", "2026-03-31"),
        )
    if scalar_count(db, "assessment_items") == 0:
        db.executemany(
            "INSERT INTO assessment_items(code,title,prompt,grade_s,grade_a,grade_b,grade_c) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    "TEAM_CONTRIBUTION",
                    "Team Goal Contribution",
                    "Did this person contribute to agreed team priorities?",
                    "Decisive impact beyond role.",
                    "Solid contribution in agreed role.",
                    "Delivery with frequent guidance needed.",
                    "Low relevance or low quality output.",
                ),
                (
                    "TASK_ACHIEVEMENT",
                    "Core Task Achievement",
                    "Did this person achieve expected outcomes in three months?",
                    "Exceeded target significantly.",
                    "Met target and quality expectations.",
                    "Around 80 percent or quality/timeline gaps.",
                    "Significant miss or unusable quality.",
                ),
                (
                    "BEHAVIOR_ALIGNMENT",
                    "Behavior Alignment",
                    "Did this person follow expected behaviors?",
                    "Role model plus positive influence.",
                    "Consistent behavior alignment.",
                    "Mostly aligned with occasional correction needed.",
                    "Repeated misalignment without improvement.",
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
        return "?????? ???? ????? ???????."
    comments = [r["peer_comment"] for r in rows]
    return f"?? {len(comments)}?? ????. ??? ???: {'; '.join(comments[:3])}"


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
            "1. ??? ????? ??? ?????? ??? ??????? ??? ?????? ?????.\n"
            "2. ???? ??????? ?????? ?????? ??? ?????? ????????????\n"
            "3. ???? ?????? ?????? ???? ?????????\n"
            "4. ???? ?????? ??? ???????? ?? ???? ????????\n"
            "5. ???? 90?? ?????? ???? ???? ??? 1?????? ??????????"
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


@app.route("/")
def index():
    return redirect(url_for("dashboard" if current_user() else "login"))


@app.before_request
def bootstrap():
    ensure_db_initialized()


@app.route("/login", methods=["GET", "POST"])
def login():
    db = get_db()
    if request.method == "POST":
        user_id = request.form.get("user_id")
        if db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            session["user_id"] = int(user_id)
            return redirect(url_for("dashboard"))
    users = db.execute("SELECT * FROM users ORDER BY role, id").fetchall()
    return render_template_string(
        """
        <h2>?????? ????? ??????</h2>
        <form method="post">
          <select name="user_id">
            {% for u in users %}
              <option value="{{u.id}}">{{u.name}} ({{u.role}})</option>
            {% endfor %}
          </select>
          <button type="submit">??????</button>
        </form>
        """,
        users=users,
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
        """
        <h2>?????? ??????</h2>
        <a href="{{url_for('logout')}}">??????</a>
        <h3>?????? ???? ???</h3>
        <form method="post">
          <input type="hidden" name="form_type" value="policy"/>
          <select name="peer_visibility">
            {% for key, label in peer_visibility_options.items() %}
              <option value="{{key}}" {% if key == peer_visibility %}selected{% endif %}>{{label}}</option>
            {% endfor %}
          </select>
          <button type="submit">??? ????</button>
        </form>
        <h3>?? ????? ????</h3>
        <form method="post">
          <label>?????</label>
          <select name="target_user_id">{% for t in targets %}<option value="{{t.id}}">{{t.name}}</option>{% endfor %}</select>
          <label>?? ?????</label>
          <select name="cycle_id">{% for c in cycles %}<option value="{{c.id}}">{{c.name}}</option>{% endfor %}</select>
          <fieldset>
            <legend>???? (??? 3??)</legend>
            {% for e in evaluators %}
              <label><input type="checkbox" name="evaluator_ids" value="{{e.id}}"> {{e.name}}</label><br/>
            {% endfor %}
          </fieldset>
          <button type="submit">????</button>
        </form>
        <h3>????? ???</h3>
        <table border="1" cellpadding="6">
          <tr><th>ID</th><th>?????</th><th>?????</th><th>???? ???? ???</th><th>PT ????</th><th>????</th><th>???</th></tr>
          {% for e in evaluatees %}
          <tr>
            <td>{{e.id}}</td><td>{{e.target_name}}</td><td>{{e.cycle_name}}</td><td>/peer-survey/{{e.peer_survey_token}}</td>
            <td>{{e.presentation_filename or '-'}}</td><td>{{e.decision or 'IN_PROGRESS'}}</td>
            <td><a href="{{url_for('aggregate_result', evaluatee_id=e.id)}}">????</a> | <a href="{{url_for('deliver_feedback', evaluatee_id=e.id)}}">????</a></td>
          </tr>
          {% endfor %}
        </table>
        """,
        targets=targets,
        evaluators=evaluators,
        cycles=cycles,
        evaluatees=evaluatees,
        peer_visibility=peer_visibility,
        peer_visibility_options=PEER_VISIBILITY_OPTIONS,
    )


@app.route("/target", methods=["GET", "POST"])
@require_role("target")
def target_dashboard():
    db = get_db()
    user = current_user()
    evaluatee = db.execute("SELECT * FROM evaluatees WHERE user_id=? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
    if not evaluatee:
        return "?????? ??? ???????."
    items = db.execute("SELECT * FROM assessment_items ORDER BY id").fetchall()
    existing = db.execute("SELECT * FROM self_assessments WHERE evaluatee_id=?", (evaluatee["id"],)).fetchall()
    existing_by_item = {row["item_id"]: row for row in existing}
    if request.method == "POST":
        file = request.files.get("presentation")
        if file and file.filename:
            filename = f"{evaluatee['id']}_{secure_filename(file.filename)}"
            file.save(UPLOAD_DIR / filename)
            db.execute("UPDATE evaluatees SET presentation_filename=? WHERE id=?", (filename, evaluatee["id"]))
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
        return redirect(url_for("target_dashboard"))
    result = db.execute("SELECT * FROM aggregated_results WHERE evaluatee_id=?", (evaluatee["id"],)).fetchone()
    return render_template_string(
        """
        <h2>?? ????? ???</h2>
        <a href="{{url_for('logout')}}">??????</a>
        <h3>PT ?????? + ?????</h3>
        <form method="post" enctype="multipart/form-data">
          <input type="file" name="presentation"/><br/><br/>
          {% for item in items %}
            <div style="border:1px solid #ccc; padding:8px; margin:8px 0;">
              <b>{{item.title}}</b><br/><small>{{item.prompt}}</small><br/>
              {% set current = existing_by_item[item.id].grade if item.id in existing_by_item else 'B' %}
              {% for g in ['S','A','B','C'] %}
                <label><input type="radio" name="grade_{{item.id}}" value="{{g}}" {% if current==g %}checked{% endif %}>{{g}}</label>
              {% endfor %}
            </div>
          {% endfor %}
          <label>Keep(???? ??)</label><br/><textarea name="keep_text" rows="3" cols="90">{{existing[0].keep_text if existing else ''}}</textarea><br/>
          <label>Problem(????? ??)</label><br/><textarea name="problem_text" rows="3" cols="90">{{existing[0].problem_text if existing else ''}}</textarea><br/>
          <label>Try(???? ???)</label><br/><textarea name="try_text" rows="3" cols="90">{{existing[0].try_text if existing else ''}}</textarea><br/>
          <button type="submit">????</button>
        </form>
        <h3>???</h3>
        {% if result %}
          <p><b>????:</b> {{result.decision}}</p>
          <p><b>???:</b> {{result.summary}}</p>
          <p><b>????:</b> {{result.admin_feedback or '-'}}</p>
        {% else %}
          <p>???? ????? ??????? ???????.</p>
        {% endif %}
        """,
        items=items,
        existing=existing,
        existing_by_item=existing_by_item,
        result=result,
    )


@app.route("/uploads/<path:filename>")
def download_upload(filename):
    if not current_user():
        abort(403)
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)


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
            peer_summary = "?????? ???? ??????? ??????? ?????????."
        ai_row = db.execute(
            "SELECT suggested_questions FROM ai_question_logs WHERE evaluatee_id=? AND evaluator_user_id=? ORDER BY id DESC LIMIT 1",
            (selected, user["id"]),
        ).fetchone()
        if ai_row:
            ai_questions = ai_row["suggested_questions"]
    return render_template_string(
        """
        <h2>???? ???</h2>
        <a href="{{url_for('logout')}}">??????</a>
        <h3>???? ?????</h3>
        <ul>{% for a in assignments %}<li><a href="{{url_for('evaluator_dashboard', evaluatee_id=a.evaluatee_id)}}">{{a.target_name}}</a></li>{% endfor %}</ul>
        {% if detail %}
          <h3>{{detail.target_name}}</h3>
          <p>PT ????: {% if detail.presentation_filename %}<a href="{{url_for('download_upload', filename=detail.presentation_filename)}}">{{detail.presentation_filename}}</a>{% else %}???????{% endif %}</p>
          <p>?????? ????: {{peer_summary}}</p>
          <h4>?????</h4>
          {% for s in self_data %}
            <div style="border:1px solid #ccc; padding:8px; margin:8px 0;">
              <b>{{s.title}} - {{s.grade}}</b><br/>Keep: {{s.keep_text or '-'}}<br/>Problem: {{s.problem_text or '-'}}<br/>Try: {{s.try_text or '-'}}
            </div>
          {% endfor %}
          <h4>???? ??</h4>
          <form method="post">
            <input type="hidden" name="evaluatee_id" value="{{detail.id}}"/>
            {% for item in items %}
              {% set current = leader_data[item.id].grade if item.id in leader_data else 'B' %}
              <div style="border:1px solid #999; padding:8px; margin:8px 0;">
                <b>{{item.title}}</b><br/><small>{{item.prompt}}</small><br/>
                {% for g in ['S','A','B','C'] %}
                  <label><input type="radio" name="grade_{{item.id}}" value="{{g}}" {% if g==current %}checked{% endif %}>{{g}}</label>
                {% endfor %}
              </div>
            {% endfor %}
            <label>PT ???</label><br/><textarea name="presentation_note" rows="3" cols="90">{{ leader_data.values()|list|first.presentation_note if leader_data else '' }}</textarea><br/>
            <label>Q&A ???</label><br/><textarea name="qa_note" rows="3" cols="90">{{ leader_data.values()|list|first.qa_note if leader_data else '' }}</textarea><br/>
            <label>????? ???? ????</label><br/><textarea name="feedback_text" rows="4" cols="90">{{ leader_data.values()|list|first.feedback_text if leader_data else '' }}</textarea><br/>
            <button type="submit">????</button>
          </form>
          <h4>AI ???? ????</h4>
          <form method="post" action="{{url_for('ai_questions', evaluatee_id=detail.id)}}">
            <textarea name="source_note" rows="4" cols="90" placeholder="???/???????? ??? ?????? ???????? ????????."></textarea><br/>
            <button type="submit">???? ????</button>
          </form>
          {% if ai_questions %}<pre>{{ai_questions}}</pre>{% endif %}
        {% endif %}
        """,
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
    if not evaluatee:
        abort(404)
    if request.method == "POST":
        comment = request.form.get("peer_comment", "").strip()
        peer_name = request.form.get("peer_name", "").strip()
        if comment:
            db.execute(
                "INSERT INTO peer_surveys(evaluatee_id,peer_name,peer_comment,created_at) VALUES (?,?,?,?)",
                (evaluatee["id"], peer_name, comment, now()),
            )
            db.commit()
            return "???????????."
    return render_template_string(
        """
        <h2>???? ?? ????</h2>
        <p>?????: {{evaluatee.target_name}}</p>
        <form method="post">
          <label>??? (????)</label><br/><input type="text" name="peer_name"/><br/>
          <label>???</label><br/><textarea name="peer_comment" rows="6" cols="80" required></textarea><br/>
          <button type="submit">????</button>
        </form>
        """,
        evaluatee=evaluatee,
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
        peer_text = "?????? ????? ?????? ????? ???????."
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
        return "???? ?????? ?????? ?????."
    if request.method == "POST":
        admin_feedback = request.form.get("admin_feedback", "")
        db.execute(
            "UPDATE aggregated_results SET admin_feedback=?, delivered_at=?, updated_at=? WHERE evaluatee_id=?",
            (admin_feedback, now(), now(), evaluatee_id),
        )
        db.commit()
        return redirect(url_for("admin_dashboard"))
    return render_template_string(
        """
        <h2>??? ????</h2>
        <p>????: {{result.decision}}</p>
        <p>???: {{result.summary}}</p>
        <form method="post">
          <textarea name="admin_feedback" rows="6" cols="90">{{result.admin_feedback or ''}}</textarea><br/>
          <button type="submit">????</button>
        </form>
        """,
        result=result,
    )


@app.route("/health")
def health():
    return {"status": "ok", "time": now()}


if __name__ == "__main__":
    ensure_db_initialized()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
