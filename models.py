"""SQLite-модели и доступ к данным приложения."""

import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import g
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Load the project's .env before any settings are read. Real process variables
# take priority, so systemd/Docker configuration is never overwritten.
load_dotenv(os.path.join(BASE_DIR, ".env"), override=False)

DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "career.db"))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
TZ = 5  # GMT+5, Ашхабад

ALLOWED_EXT = {"pdf", "doc", "docx", "jpg", "jpeg", "png"}
ALLOWED_LABEL = {
    "none": "Без опыта",
    "lt1": "Меньше 1 года",
    "y1_3": "1–3 года",
    "y3_5": "3–5 лет",
    "gt5": "Больше 5 лет",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS sites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    slug       TEXT UNIQUE,
    token      TEXT UNIQUE NOT NULL,
    url        TEXT,
    vacancy    TEXT,
    is_active  INTEGER DEFAULT 1,
    is_closed  INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS applications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id       INTEGER REFERENCES sites(id) ON DELETE SET NULL,
    name          TEXT NOT NULL,
    phone         TEXT NOT NULL,
    email         TEXT,
    city          TEXT,
    experience    TEXT,
    message       TEXT,
    file_name     TEXT,
    file_storage  TEXT,
    file_size     INTEGER,
    status        TEXT DEFAULT 'new',
    tg_ok         INTEGER DEFAULT 0,
    tg_error      TEXT,
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS admins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    login         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_app_site ON applications(site_id);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 15000")
    return g.db


def close_db(e=None):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    db_parent = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(db_parent, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    try:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


# ------------------------------------------------------------- settings ----
def get_setting(key):
    row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key, value):
    db().execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db().commit()


# ---------------------------------------------------------------- sites ----
def get_site(key, by="token"):
    col = {"token": "token", "slug": "slug", "id": "id"}.get(by)
    if col is None:
        return None
    try:
        key = str(key)
    except (TypeError, ValueError):
        return None
    if not key:
        return None
    return db().execute(f"SELECT * FROM sites WHERE {col}=?", (key,)).fetchone()


# ---------------------------------------------------------- applications ---
def get_site_for_apply(app_id):
    return db().execute(
        """SELECT s.* FROM applications a JOIN sites s ON s.id=a.site_id
           WHERE a.id=?""", (app_id,)
    ).fetchone()


# ---------------------------------------------------------------- admins ---
def get_admin_by_login(login):
    if not login:
        return None
    return db().execute("SELECT * FROM admins WHERE login=?", (login,)).fetchone()


def create_admin(login, password):
    created_at = datetime.now(timezone(timedelta(hours=TZ))).isoformat()
    db().execute(
        "INSERT INTO admins (login, password_hash, created_at) VALUES (?,?,?)",
        (login, generate_password_hash(password), created_at),
    )
    db().commit()


def seed_default_admin():
    login = os.environ.get("ADMIN_LOGIN", "admin").strip() or "admin"
    password = os.environ.get("ADMIN_PASSWORD", "")
    if get_admin_by_login(login) is not None:
        return

    generated = not password
    if generated:
        password = secrets.token_urlsafe(15)
    elif len(password) < 8:
        raise RuntimeError("ADMIN_PASSWORD должен содержать минимум 8 символов.")

    try:
        create_admin(login, password)
    except sqlite3.IntegrityError:
        # Another Gunicorn worker may have created the same initial admin.
        db().rollback()
        return

    if generated:
        print(
            "[!] ADMIN_PASSWORD не задан. Создан администратор:\n"
            f"    логин: {login}\n"
            f"    пароль: {password}\n"
            "    Сохраните пароль и смените его после входа в панель."
        )
