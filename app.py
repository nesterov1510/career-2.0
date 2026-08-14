"""MSB Career — анкеты вакансий с отправкой в Telegram и админ-панелью."""

import html as html_mod
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from threading import Lock
from urllib.parse import urlsplit

import requests
from flask import (
    Blueprint,
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFError, CSRFProtect
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from models import (
    ALLOWED_EXT,
    ALLOWED_LABEL,
    DB_PATH,
    TZ,
    UPLOAD_DIR,
    close_db,
    db,
    get_admin_by_login,
    get_setting,
    get_site,
    init_db,
    seed_default_admin,
    set_setting,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _secret_key():
    """Return one stable key, including when several Gunicorn workers start."""
    configured = os.environ.get("SECRET_KEY", "").strip()
    if configured:
        if len(configured) < 32:
            print("[WARN] SECRET_KEY короче 32 символов; используйте длинный случайный ключ.")
        return configured

    key_file = os.environ.get(
        "SECRET_KEY_FILE", os.path.join(os.path.dirname(DB_PATH) or BASE_DIR, ".secret_key")
    )
    os.makedirs(os.path.dirname(os.path.abspath(key_file)), exist_ok=True)
    try:
        with open(key_file, encoding="ascii") as f:
            saved = f.read().strip()
        if saved:
            return saved
    except FileNotFoundError:
        pass

    generated = secrets.token_hex(32)
    try:
        # Exclusive creation prevents workers from overwriting one another's key.
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(generated)
        print(f"[WARN] SECRET_KEY не задан; постоянный ключ создан в {key_file}")
        return generated
    except FileExistsError:
        # The winning worker may still be writing the just-created file.
        for _ in range(20):
            with open(key_file, encoding="ascii") as f:
                saved = f.read().strip()
            if saved:
                return saved
            time.sleep(0.05)
        raise RuntimeError(f"Не удалось прочитать SECRET_KEY_FILE: {key_file}")


SECRET_KEY = _secret_key()
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # за nginx
app.config.update(
    SECRET_KEY=SECRET_KEY,
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower()
    in ("1", "true", "yes"),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
csrf = CSRFProtect(app)  # публичная форма защищена отдельным временным токеном
app.teardown_appcontext(close_db)


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    if request.is_secure:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


public_signer = URLSafeTimedSerializer(SECRET_KEY, salt="public-form")

PANEL = os.environ.get("PANEL_PATH", "/x-panel-7f3a").strip()
if not PANEL.startswith("/"):
    PANEL = "/" + PANEL
PANEL = PANEL.rstrip("/")
if not PANEL or not re.fullmatch(r"/[A-Za-z0-9_-]+", PANEL):
    raise RuntimeError("PANEL_PATH должен иметь вид /secret-panel (без вложенных путей)")

MAX_FILE = 5 * 1024 * 1024
TIMEZONE = timezone(timedelta(hours=TZ))

# ---------------------------------------------------------------- утилиты ---
def now():
    return datetime.now(TIMEZONE)


def slugify(text):
    translit = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
        "ә": "a", "җ": "j", "ң": "n", "ө": "o", "ү": "u", "һ": "h", "ӣ": "i",
    }
    text = (text or "").lower().strip()
    out = []
    for ch in text:
        if re.match(r"[a-z0-9]", ch):
            out.append(ch)
        elif ch in translit:
            out.append(translit[ch])
        elif ch in " -_/\\.":
            out.append("-")
    slug = re.sub(r"-{2,}", "-", "".join(out)).strip("-")
    return slug or secrets.token_hex(3)


def gen_token():
    return secrets.token_urlsafe(12)


def find_site(token_or_slug):
    return get_site(token_or_slug) or get_site(token_or_slug, by="slug")


def public_token():
    return public_signer.dumps("form")


def check_public_token(tok):
    try:
        return public_signer.loads(tok, max_age=7200) == "form"
    except BadSignature:
        return False


def phone_display(p):
    p = (p or "").strip()
    m = re.match(r"^(\+\d{3})(\d{2})(\d{6})$", p)          # TM: +993 63 123456
    if m:
        return f"{m.group(1)} {m.group(2)} {m.group(3)[:2]}-{m.group(3)[2:4]}-{m.group(3)[4:]}"
    m = re.match(r"^(\+\d)(\d{3})(\d{3})(\d{2})(\d{2})$", p)  # RU: +7 912 345-67-89
    if m:
        return f"{m.group(1)} {m.group(2)} {m.group(3)}-{m.group(4)}-{m.group(5)}"
    return p


def fmt_dt(s):
    try:
        return datetime.fromisoformat(s).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return s or ""


def clean_url(value):
    """Accept only safe absolute web URLs (or an empty value)."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    return value


def human_size(n):
    if not n:
        return ""
    n = int(n)
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} МБ"
    if n >= 1024:
        return f"{n // 1024} КБ"
    return f"{n} Б"


def tg_call(method, files=None, bot_token=None, **data):
    token = ((bot_token if bot_token is not None else get_setting("bot_token")) or "").strip()
    if not token:
        return {"ok": False, "error": "Bot token не задан в настройках панели"}
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            data=data,
            files=files,
            timeout=25,
        )
        try:
            result = response.json()
        except ValueError:
            return {"ok": False, "error": f"Telegram вернул HTTP {response.status_code}"}
        if response.ok:
            return result
        result.setdefault("ok", False)
        result.setdefault("error", f"Telegram вернул HTTP {response.status_code}")
        return result
    except requests.RequestException as exc:
        return {"ok": False, "error": f"Сеть: {exc}"}


def send_application(app_id):
    """Отправка заявки в Telegram-группу. Возвращает (ok, text)."""
    row = db().execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if row is None:
        return False, "Заявка не найдена"
    site = get_site(row["site_id"]) if row["site_id"] else None
    chat_id = (get_setting("chat_id") or "").strip()
    if not chat_id:
        return False, "chat_id группы не задан в настройках панели"
    topic_id = (get_setting("topic_id") or "").strip()
    destination = {"chat_id": chat_id}
    if topic_id:
        destination["message_thread_id"] = topic_id

    about = (row["message"] or "").strip()
    if len(about) > 3000:
        about = about[:3000] + "…"

    lines = [
        "🆕 <b>Новая анкета на вакансию</b>",
        "",
        f"🌐 Сайт: <b>{html_mod.escape(site['name'] if site else 'неизвестен')}</b>",
        f"🔗 {html_mod.escape(site['url'] if site and site['url'] else '—')}",
    ]
    if site and site["vacancy"]:
        lines.append(f"💼 Вакансия: {html_mod.escape(site['vacancy'])}")
    lines += [
        "",
        f"👤 ФИО: <b>{html_mod.escape(row['name'])}</b>",
        f"📞 Телефон: <b>{html_mod.escape(phone_display(row['phone']))}</b>",
    ]
    if row["email"]:
        lines.append(f"✉️ Email: {html_mod.escape(row['email'])}")
    lines.append(f"🛠 Опыт: {html_mod.escape(ALLOWED_LABEL.get(row['experience'], row['experience']))}")
    if row["city"]:
        lines.append(f"📍 Город: {html_mod.escape(row['city'])}")
    if about:
        lines += ["", f"💬 О себе:\n{html_mod.escape(about)}"]
    if row["file_name"]:
        lines.append(f"📎 Документ: {html_mod.escape(row['file_name'])} ({human_size(row['file_size'])})")
    lines += [
        "",
        f"🕒 {now().strftime('%d.%m.%Y %H:%M')} (GMT+{TZ})",
        f"🆔 Заявка №{row['id']}",
        f"#заявка #{slugify(site['name']) if site else 'site'}",
    ]
    caption = "\n".join(lines)

    if row["file_storage"]:
        fpath = os.path.join(UPLOAD_DIR, row["file_storage"])
        if not os.path.isfile(fpath):
            return False, "Прикреплённый файл не найден на сервере"

        # Telegram limits document captions to 1024 characters. For a long
        # application, send the file first with a short caption, then details.
        document_caption = caption
        details_required = len(caption) > 1000
        if details_required:
            document_caption = (
                f"📎 Документ к заявке №{row['id']}\n"
                f"👤 {html_mod.escape(row['name'])}"
            )
        with open(fpath, "rb") as uploaded:
            res = tg_call(
                "sendDocument",
                **destination,
                caption=document_caption,
                parse_mode="HTML",
                files={"document": (row["file_name"] or "document", uploaded)},
            )
        if not res.get("ok"):
            desc = res.get("description") or res.get("error") or "ошибка отправки файла"
            return False, desc
        if not details_required:
            return True, "ok"

    res = tg_call(
        "sendMessage",
        **destination,
        text=caption,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    if res.get("ok"):
        return True, "ok"
    desc = (res.get("description") or res.get("error") or "ошибка")
    return False, desc


# ------------------------------------------------------- анти-спам (простой) ---
_submits, _logins = {}, {}
_lock = Lock()


def _rate(key, store, limit, window):
    t = time.time()
    with _lock:
        arr = [x for x in store.get(key, []) if x > t - window]
        if len(arr) >= limit:
            store[key] = arr
            return False
        arr.append(t)
        store[key] = arr
        return True


# ---------------------------------------------------------------- шаблон ----
@app.context_processor
def inject_globals():
    return {
        "panel": PANEL,
        "current_year": now().year,
        "fmt_dt": fmt_dt,
        "human_size": human_size,
        "phone_display": phone_display,
        "ALLOWED_LABEL": ALLOWED_LABEL,
        "now": now,
    }


# ============================================================== ПУБЛИЧНАЯ ЧАСТЬ
@app.route("/")
def index():
    return render_template("home.html")


@app.route("/robots.txt")
def robots():
    return ("User-agent: *\n"
            f"Disallow: {PANEL}/\n"
            "Disallow: /apply/\n"), 200, {"Content-Type": "text/plain"}


@app.route("/apply/<token_or_slug>/", methods=["GET"])
def apply_form(token_or_slug):
    site = find_site(token_or_slug)
    if not site or not site["is_active"]:
        return render_template("apply_closed.html", reason="closed"), 404
    if site["is_closed"]:
        return render_template("apply_closed.html", reason="closed"), 410
    lang = request.args.get("lang", "ru")
    if lang not in ("ru", "tm"):
        lang = "ru"
    return render_template("apply.html", site=site, lang=lang, form_token=public_token())


@app.route("/apply/<token_or_slug>/", methods=["POST"])
@csrf.exempt
def handle_apply(token_or_slug):
    site = find_site(token_or_slug)
    if not site or not site["is_active"]:
        return render_template("apply_closed.html", reason="closed"), 404
    if site["is_closed"]:
        return render_template("apply_closed.html", reason="closed"), 410

    lang = request.form.get("lang", "ru")
    if lang not in ("ru", "tm"):
        lang = "ru"

    if request.form.get("website"):  # honeypot
        return redirect(url_for("apply_success"))
    if not check_public_token(request.form.get("form_token", "")):
        return render_template("apply.html", site=site, lang=lang,
                               form_token=public_token(),
                               error=("Сессия формы истекла — обновите страницу и попробуйте снова."
                                      if lang == "ru" else "Sessiýanyň möhleti gutardy — sahypany täzeläp, gaýtadan synanyşyň.")), 400

    if not _rate(request.remote_addr or "?", _submits, 5, 600):
        return render_template("apply.html", site=site, lang=lang,
                               form_token=public_token(),
                               error=("Слишком много отправок. Попробуйте через 10 минут."
                                      if lang == "ru" else "Gaty köp gönderme. 10 minutdan soň synanyşyň.")), 429

    name = re.sub(r"\s+", " ", (request.form.get("name") or "")).strip()
    email = (request.form.get("email") or "").strip()
    prefix = (request.form.get("prefix") or "+993").strip()
    allowed_prefixes = {"+993", "+7", "+998"}
    digits = re.sub(r"\D", "", request.form.get("phone", ""))
    phone = prefix + digits
    city = (request.form.get("city") or "").strip()
    experience = request.form.get("experience", "")
    message = (request.form.get("message") or "").strip()

    errors = {}
    if len(name) < 2 or len(name) > 120:
        errors["name"] = "Укажите имя и фамилию" if lang == "ru" else "Adyňyzy we familiýaňyzy ýazyň"
    if prefix not in allowed_prefixes or not 5 <= len(digits) <= 12:
        errors["phone"] = "Укажите номер телефона" if lang == "ru" else "Telefon belgiňizi ýazyň"
    if experience not in ALLOWED_LABEL:
        errors["experience"] = "Выберите опыт работы" if lang == "ru" else "Iş tejribäňizi saýlaň"
    if len(email) > 120 or (email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
        errors["email"] = "Неверный e-mail" if lang == "ru" else "E-nädogry ýazylan"
    if len(city) > 80:
        errors["city"] = "Название города слишком длинное" if lang == "ru" else "Şäheriň ady gaty uzyn"
    if len(message) > 2000:
        errors["message"] = "Слишком длинный текст (макс. 2000)" if lang == "ru" else "Tekst gaty uzyn (iň köp 2000)"

    file = request.files.get("file")
    file_ok = bool(file and file.filename and file.filename.rsplit(".", 1)[0])
    fsize = 0
    if file_ok:
        ext = file.filename.rsplit(".", 1)[-1].lower()
        if ext not in ALLOWED_EXT:
            errors["file"] = ("Можно: PDF, DOC(X), JPG, PNG" if lang == "ru"
                              else "Rugsat: PDF, DOC(X), JPG, PNG")
        else:
            file.seek(0, os.SEEK_END)
            fsize = file.tell()
            file.seek(0)
            if fsize > MAX_FILE:
                errors["file"] = ("Файл больше 5 МБ" if lang == "ru" else "Faýl 5 MB-den uly")
            elif fsize == 0:
                errors["file"] = ("Пустой файл" if lang == "ru" else "Boş faýl")
        if "file" in errors:
            file_ok = False

    if errors:
        return render_template("apply.html", site=site, lang=lang,
                               form_token=public_token(), errors=errors,
                               form=request.form), 400

    storage_name = None
    storage_path = None
    if file_ok:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        storage_name = f"{uuid.uuid4().hex}.{ext}"
        storage_path = os.path.join(UPLOAD_DIR, storage_name)
        file.save(storage_path)

    try:
        cur = db().execute(
            """INSERT INTO applications (site_id, name, phone, email, city, experience,
                                         message, file_name, file_storage, file_size,
                                         status, tg_ok, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?, 'new', 0, ?)""",
            (site["id"], name, phone, email, city, experience,
             message, file.filename[:180] if file_ok else None,
             storage_name, fsize if file_ok else None, now().isoformat()),
        )
        db().commit()
    except sqlite3.DatabaseError:
        db().rollback()
        if storage_path:
            try:
                os.remove(storage_path)
            except OSError:
                pass
        raise
    app_id = cur.lastrowid

    ok, info = send_application(app_id)
    db().execute(
        "UPDATE applications SET tg_ok=?, tg_error=? WHERE id=?",
        (1 if ok else 0, None if ok else info[:400], app_id),
    )
    db().commit()

    session["submitted_id"] = app_id
    return redirect(url_for("apply_success", lang=lang))


@app.route("/apply/success/")
def apply_success():
    app_id = session.pop("submitted_id", None)
    lang = request.args.get("lang", "ru")
    if lang not in ("ru", "tm"):
        lang = "ru"
    if not app_id:
        return redirect(url_for("index"))
    return render_template("success.html", app_id=app_id, lang=lang)


# ============================================================== АДМИН-ПАНЕЛЬ ===
admin = Blueprint("admin", __name__, url_prefix=PANEL)


def logged():
    return session.get("admin_id") is not None


def auth_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not logged():
            return redirect(url_for("admin.login", next=request.path))
        return fn(*a, **kw)

    return wrapper


@admin.app_context_processor
def panel_globals():
    return {
        "panel": PANEL,
        "logged": logged(),
        "active": request.endpoint,
        "secure_cookie_on_http": (
            app.config["SESSION_COOKIE_SECURE"] and not request.is_secure
        ),
    }


# ------------------------------------------------------------------ вход -----
@admin.route("/login/", methods=["GET", "POST"])
def login():
    if logged():
        return redirect(url_for("admin.dashboard"))
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if not _rate(ip, _logins, 6, 900):
            flash("Слишком много попыток входа. Попробуйте через 15 минут.", "err")
            return render_template("panel/login.html"), 429
        a = get_admin_by_login(request.form.get("login", "").strip())
        if a and check_password_hash(a["password_hash"], request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["admin_id"] = a["id"]
            session["admin_login"] = a["login"]
            nxt = request.args.get("next") or ""
            if not (nxt == PANEL or nxt.startswith(PANEL + "/")):
                nxt = ""
            return redirect(nxt or url_for("admin.dashboard"))
        flash("Неверный логин или пароль.", "err")
    return render_template("panel/login.html")


@admin.route("/logout/", methods=["POST"])
@auth_required
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


# -------------------------------------------------------------- дашборд -----
@admin.route("/")
@auth_required
def dashboard():
    total = db().execute("SELECT COUNT(*) c FROM applications").fetchone()["c"]
    new = db().execute("SELECT COUNT(*) c FROM applications WHERE status='new'").fetchone()["c"]
    sites = db().execute("SELECT COUNT(*) c FROM sites").fetchone()["c"]
    today = now().date().isoformat()
    today_n = db().execute(
        "SELECT COUNT(*) c FROM applications WHERE substr(created_at,1,10)=?", (today,)
    ).fetchone()["c"]
    recent = db().execute(
        """SELECT a.*, s.name AS site_name FROM applications a
           LEFT JOIN sites s ON s.id = a.site_id
           ORDER BY a.id DESC LIMIT 7"""
    ).fetchall()
    return render_template("panel/dashboard.html",
                           total=total, new=new, sites=sites, today_n=today_n, recent=recent)


# ----------------------------------------------------------------- сайты -----
@admin.route("/sites/", methods=["GET", "POST"])
@auth_required
def sites():
    if request.method == "POST":
        action = request.form.get("action")
        name = re.sub(r"\s+", " ", (request.form.get("name") or "")).strip()
        site_url = clean_url(request.form.get("url"))
        vacancy = (request.form.get("vacancy") or "").strip()
        if action == "add":
            if not name or len(name) > 140:
                flash("Укажите название сайта (не более 140 символов).", "err")
            elif site_url is None or len(site_url) > 300:
                flash("Укажите корректный адрес сайта с http:// или https://.", "err")
            elif len(vacancy) > 200:
                flash("Название вакансии не должно превышать 200 символов.", "err")
            else:
                slug = base = slugify(name)
                i = 2
                while get_site(slug, by="slug"):
                    slug = f"{base}-{i}"
                    i += 1
                db().execute(
                    "INSERT INTO sites (name, slug, token, url, vacancy, is_active, is_closed, created_at) "
                    "VALUES (?,?,?,?,?,1,0,?)",
                    (name, slug[:80], gen_token(), site_url, vacancy, now().isoformat()),
                )
                db().commit()
                flash("Сайт добавлен. Ссылка на анкету готова.", "ok")
            return redirect(url_for("admin.sites"))

        sid = request.form.get("site_id")
        site = get_site(sid, by="id")
        if not site:
            return redirect(url_for("admin.sites"))

        if action == "update":
            if not name or len(name) > 140:
                flash("Укажите название сайта (не более 140 символов).", "err")
            elif site_url is None or len(site_url) > 300:
                flash("Укажите корректный адрес сайта с http:// или https://.", "err")
            elif len(vacancy) > 200:
                flash("Название вакансии не должно превышать 200 символов.", "err")
            else:
                db().execute(
                    "UPDATE sites SET name=?, url=?, vacancy=?, is_closed=? WHERE id=?",
                    (name, site_url, vacancy,
                     1 if request.form.get("is_closed") else 0, sid),
                )
                db().commit()
                flash("Изменения сохранены.", "ok")
        elif action == "regen":
            db().execute("UPDATE sites SET token=? WHERE id=?", (gen_token(), sid))
            db().commit()
            flash("Токен обновлён — старая ссылка больше не работает.", "ok")
        elif action == "toggle":
            db().execute("UPDATE sites SET is_active=? WHERE id=?",
                         (0 if site["is_active"] else 1, sid))
            db().commit()
        elif action == "delete":
            for row in db().execute("SELECT file_storage FROM applications WHERE site_id=?", (sid,)):
                if row["file_storage"]:
                    try:
                        os.remove(os.path.join(UPLOAD_DIR, row["file_storage"]))
                    except OSError:
                        pass
            db().execute("DELETE FROM applications WHERE site_id=?", (sid,))
            db().execute("DELETE FROM sites WHERE id=?", (sid,))
            db().commit()
            flash("Сайт и его заявки удалены.", "ok")
        return redirect(url_for("admin.sites"))

    rows = db().execute(
        """SELECT s.*, COUNT(a.id) apps,
                  SUM(CASE WHEN a.status='new' THEN 1 ELSE 0 END) new_apps
           FROM sites s LEFT JOIN applications a ON a.site_id = s.id
           GROUP BY s.id ORDER BY s.id DESC"""
    ).fetchall()
    return render_template("panel/sites.html", rows=rows)


# ---------------------------------------------------------------- заявки ----
@admin.route("/apps/")
@auth_required
def apps():
    q_site = request.args.get("site", "")
    q_status = request.args.get("status", "")
    q = request.args.get("q", "").strip()
    sql = ("SELECT a.*, s.name AS site_name FROM applications a "
           "LEFT JOIN sites s ON s.id = a.site_id WHERE 1=1")
    args = []
    if q_site:
        sql += " AND a.site_id=?"
        args.append(q_site)
    if q_status:
        sql += " AND a.status=?"
        args.append(q_status)
    if q:
        sql += " AND (a.name LIKE ? OR a.phone LIKE ? OR a.email LIKE ? OR a.city LIKE ?)"
        like = f"%{q}%"
        args += [like, like, like, like]
    sql += " ORDER BY a.id DESC LIMIT 500"
    rows = db().execute(sql, args).fetchall()
    sites = db().execute("SELECT id, name FROM sites ORDER BY name").fetchall()
    return render_template("panel/apps.html", rows=rows, sites=sites,
                           q_site=q_site, q_status=q_status, q=q)


@admin.route("/apps/<int:aid>/", methods=["GET", "POST"])
@auth_required
def app_detail(aid):
    row = db().execute(
        """SELECT a.*, s.name AS site_name, s.url AS site_url
           FROM applications a LEFT JOIN sites s ON s.id=a.site_id
           WHERE a.id=?""", (aid,)).fetchone()
    if not row:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "status" and request.form.get("status") in ("new", "contacted", "accepted", "rejected"):
            db().execute("UPDATE applications SET status=? WHERE id=?",
                         (request.form["status"], aid))
            db().commit()
            flash("Статус обновлён.", "ok")
        elif action == "resend":
            ok, info = send_application(aid)
            db().execute("UPDATE applications SET tg_ok=?, tg_error=? WHERE id=?",
                         (1 if ok else 0, None if ok else info[:400], aid))
            db().commit()
            flash("Отправлено в Telegram ✓" if ok else f"Ошибка Telegram: {info}",
                  "ok" if ok else "err")
        elif action == "delete":
            if row["file_storage"]:
                try:
                    os.remove(os.path.join(UPLOAD_DIR, row["file_storage"]))
                except OSError:
                    pass
            db().execute("DELETE FROM applications WHERE id=?", (aid,))
            db().commit()
            flash("Заявка удалена.", "ok")
            return redirect(url_for("admin.apps"))
        return redirect(url_for("admin.app_detail", aid=aid))
    return render_template("panel/app_detail.html", row=row)


@admin.route("/apps/<int:aid>/file/")
@auth_required
def app_file(aid):
    row = db().execute("SELECT file_storage, file_name FROM applications WHERE id=?", (aid,)).fetchone()
    if not row or not row["file_storage"]:
        abort(404)
    path = os.path.join(UPLOAD_DIR, row["file_storage"])
    if not os.path.exists(path):
        abort(404)
    return send_from_directory(UPLOAD_DIR, row["file_storage"],
                               download_name=row["file_name"] or "file", as_attachment=True)


# -------------------------------------------------------------- настройки ---
def validate_telegram_settings(token, chat, topic=""):
    if token and not re.fullmatch(r"\d{5,16}:[A-Za-z0-9_-]{20,}", token):
        return "Неверный формат токена Telegram-бота. Скопируйте токен целиком из BotFather."
    if chat and not re.fullmatch(r"-?\d{1,20}", chat):
        return "Chat ID должен содержать только цифры и необязательный минус."
    if topic and not re.fullmatch(r"\d{1,20}", topic):
        return "ID темы должен быть положительным целым числом."
    return None


@admin.route("/settings/", methods=["GET", "POST"])
@auth_required
def settings():
    if request.method == "POST":
        what = request.form.get("what")
        if what == "telegram":
            token = request.form.get("bot_token", "").strip()
            chat = request.form.get("chat_id", "").strip()
            topic = request.form.get("topic_id", "").strip()
            validation_error = validate_telegram_settings(token, chat, topic)
            if validation_error:
                flash(validation_error, "err")
            else:
                set_setting("bot_token", token)
                set_setting("chat_id", chat)
                set_setting("topic_id", topic)
                flash("Настройки Telegram сохранены.", "ok")
        elif what == "password":
            a = db().execute("SELECT * FROM admins WHERE id=?",
                             (session.get("admin_id"),)).fetchone()
            cur_pw = request.form.get("current", "")
            new_pw = request.form.get("new", "")
            if not a or not check_password_hash(a["password_hash"], cur_pw):
                flash("Текущий пароль неверный.", "err")
            elif len(new_pw) < 8:
                flash("Новый пароль — минимум 8 символов.", "err")
            else:
                db().execute("UPDATE admins SET password_hash=? WHERE id=?",
                             (generate_password_hash(new_pw), a["id"]))
                db().commit()
                flash("Пароль изменён.", "ok")
        return redirect(url_for("admin.settings"))
    return render_template(
        "panel/settings.html",
        bot_token=get_setting("bot_token") or "",
        chat_id=get_setting("chat_id") or "",
        topic_id=get_setting("topic_id") or "",
        admin_login=session.get("admin_login", "admin"),
    )


@admin.route("/settings/test/", methods=["POST"])
@auth_required
def settings_test():
    token = request.form.get("bot_token", "").strip()
    chat_id = request.form.get("chat_id", "").strip()
    topic_id = request.form.get("topic_id", "").strip()
    validation_error = validate_telegram_settings(token, chat_id, topic_id)
    if validation_error:
        return jsonify(ok=False, error=validation_error)
    if not token:
        return jsonify(ok=False, error="Сначала укажите токен бота.")
    if not chat_id:
        return jsonify(ok=False, error="Сначала укажите Chat ID группы.")
    destination = {"chat_id": chat_id}
    if topic_id:
        destination["message_thread_id"] = topic_id
    res = tg_call(
        "sendMessage",
        bot_token=token,
        **destination,
        text="✅ Тестовое сообщение: анкеты MSB Career подключены.",
        disable_web_page_preview=True,
    )
    if res.get("ok"):
        return jsonify(ok=True, msg="Сообщение отправлено в группу или выбранную тему ✓")
    desc = res.get("description") or res.get("error") or "ошибка"
    return jsonify(ok=False, error=desc)


@admin.route("/settings/botinfo/", methods=["POST"])
@auth_required
def settings_botinfo():
    token = request.form.get("bot_token", "").strip()
    validation_error = validate_telegram_settings(token, "")
    if validation_error:
        return jsonify(ok=False, error=validation_error)
    if not token:
        return jsonify(ok=False, error="Сначала укажите токен бота.")
    res = tg_call("getMe", bot_token=token)
    if res.get("ok"):
        me = res["result"]
        return jsonify(ok=True, msg=f"Бот найден: @{me.get('username')} ({me.get('first_name')})")
    return jsonify(ok=False, error=res.get("description") or res.get("error") or "ошибка")


# ------------------------------------------------------------------ ошибки ---
@app.errorhandler(CSRFError)
def csrf_failure(error):
    secure_on_http = app.config["SESSION_COOKIE_SECURE"] and not request.is_secure
    if secure_on_http:
        message = (
            "Сессионная cookie не передаётся по HTTP. Установите "
            "SESSION_COOKIE_SECURE=false в .env и перезапустите приложение."
        )
    else:
        message = (
            "Сессия формы отсутствует или истекла. Обновите страницу входа и "
            "попробуйте снова. Если ошибка повторяется, разрешите cookie для сайта."
        )
    app.logger.warning("CSRF error on %s: %s", request.path, error.description)
    return render_template(
        "panel/login.html",
        csrf_error=message,
        secure_cookie_on_http=secure_on_http,
    ), 400


@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404


@app.errorhandler(413)
def too_large(_error):
    # Do not access request.form here: parsing the oversized body raises 413 again.
    lang = request.args.get("lang", "ru")
    message = (
        "Размер запроса слишком большой. Файл должен быть не больше 5 МБ."
        if lang != "tm"
        else "Talabyň ölçegi gaty uly. Faýl 5 MB-den uly bolmaly däl."
    )
    return render_template("apply_closed.html", reason="error", message=message), 413


app.register_blueprint(admin)

if __name__ == "__main__":
    init_db()
    with app.app_context():
        seed_default_admin()
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug)
