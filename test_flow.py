# Интеграционный тест полного цикла: панель → сайт → анкета → заявка в БД
import io
import os
import re
import sqlite3
import sys

import requests

BASE = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:5040").rstrip("/")
PANEL = os.environ.get("PANEL_PATH", "/x-panel-7f3a").rstrip("/")
DB_PATH = os.environ.get("DB_PATH", "data/career.db")
ok_count = 0


def check(name, cond, extra=""):
    global ok_count
    status = "✓" if cond else "✗"
    print(f"  {status} {name}" + (f" — {extra}" if extra and not cond else ""))
    if cond:
        ok_count += 1
    else:
        sys.exit(f"FAILED at: {name} {extra}")


s = requests.Session()

print("== Публичная часть ==")
r = s.get(BASE + "/")
check("главная открывается", r.status_code == 200 and "MSB" in r.text)
check(
    "SEO-заголовок главной",
    "<h1>Работа в Туркменистане и Ашхабаде</h1>" in r.text
    and "Работа в Туркменистане и Ашхабаде — свежие вакансии" in r.text,
)
r = s.get(BASE + "/robots.txt")
check("robots.txt закрывает панель", PANEL in r.text)

print("== Вход в панель ==")
r = s.get(BASE + PANEL + "/login/")
check("страница входа", r.status_code == 200)
tok = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
r = s.post(BASE + PANEL + "/login/",
           data={"csrf_token": tok, "login": "admin", "password": "WRONG"})
check("неверный пароль отклонён", "Неверный логин" in r.text)
r = s.get(BASE + PANEL + "/login/")
tok = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
r = s.post(BASE + PANEL + "/login/",
           data={"csrf_token": tok, "login": "admin", "password": "admin123"})
check("вход с верным паролем", r.status_code == 200 and "Дашборд" in r.text)

print("== Создание сайта ==")
r = s.get(BASE + PANEL + "/sites/")
tok = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
r = s.post(BASE + PANEL + "/sites/", data={
    "csrf_token": tok, "action": "add",
    "name": "TV Repair by Meryosab",
    "url": "https://tvrepair.meryosab.com/vacancies/",
    "vacancy": "Мастер по ремонту компьютеров и ноутбуков",
}, allow_redirects=True)
check("сайт добавлен", "Сайт добавлен" in r.text and "apply/" in r.text)
m = re.search(r'value="http://127\.0\.0\.1:5040/apply/([^/]+)/"', r.text)
check("ссылка с токеном сгенерирована", bool(m))
token = m.group(1)
print(f"    ссылка анкеты: /apply/{token}/")
r = s.get(BASE + "/")
check(
    "вакансия появилась на главной",
    "TV Repair by Meryosab" in r.text
    and "Мастер по ремонту компьютеров" in r.text
    and f"/apply/{token}/" in r.text
    and "Посмотреть вакансии сайта" in r.text
    and 'href="https://tvrepair.meryosab.com/vacancies/"' in r.text,
)

print("== Редактирование вакансии ==")
r = s.get(BASE + PANEL + "/sites/")
tok = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
sid = re.search(r'name="site_id" value="(\d+)"', r.text).group(1)
r = s.post(BASE + PANEL + "/sites/", data={
    "csrf_token": tok, "site_id": sid, "action": "update",
    "name": "TV Repair by Meryosab", "url": "https://tvrepair.meryosab.com/vacancies/",
    "vacancy": "Мастер по ремонту компьютеров и ноутбуков — обновлено",
}, allow_redirects=True)
check("изменения сохранены в панели", "Изменения сохранены" in r.text)
r = s.get(BASE + "/")
check("название вакансии обновилось на главной", "— обновлено" in r.text)

print("== Анкета кандидата ==")
r = s.get(f"{BASE}/apply/{token}/")
check("анкета открывается", r.status_code == 200 and "Анкета соискателя" in r.text)
check("вакансия и сайт в шапке", "Мастер по ремонту компьютеров" in r.text
      and "tvrepair.meryosab.com" in r.text)
check("переключатель TM", "?lang=tm" in r.text)
r = s.get(f"{BASE}/apply/{token}/?lang=tm")
check("туркменская версия", "Dalaşgäriň sowalnamasy" in r.text)

ftok = re.search(r'name="form_token" value="([^"]+)"', r.text).group(1)

print("== Отправка анкеты (с файлом) ==")
pdf = io.BytesIO(b"%PDF-1.4 fake resume content")
r = s.post(f"{BASE}/apply/{token}/", data={
    "form_token": ftok, "lang": "ru",
    "name": "Мердан Худайгулиев",
    "prefix": "+993", "phone": "63 123456",
    "email": "merdan@example.com",
    "city": "Ашхабад",
    "birth_date": "1995-05-20",
    "availability": "week",
    "experience": "y1_3",
    "devices_experience": "Телевизоры, мониторы и блоки питания.",
    "previous_company": "Сервисный центр",
    "previous_position": "Мастер",
    "skills": ["led_lcd_tv", "multimeter", "smd"],
    "salary": "5000",
    "message": "Ремонтирую компьютеры 2 года, работал в сервисе.",
}, files={"file": ("resume.pdf", pdf, "application/pdf")}, allow_redirects=True)
check("успех + номер заявки", r.status_code == 200 and "Анкета отправлена" in r.text and "№" in r.text)

print("== Проверка БД ==")
con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row
row = con.execute("SELECT * FROM applications ORDER BY id DESC LIMIT 1").fetchone()
check("заявка сохранена", row is not None)
app_id = row["id"]
check("ФИО", row["name"] == "Мердан Худайгулиев")
check("телефон", row["phone"] == "+99363123456")
check("файл сохранён", row["file_name"] == "resume.pdf" and row["file_storage"])
check("TG помечен как недоставленный (бот не настроен)", row["tg_ok"] == 0 and row["tg_error"])
print(f"    tg_error: {row['tg_error']}")

print("== Валидация ==")
count_before = con.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"]
r = s.get(f"{BASE}/apply/{token}/")
ftok = re.search(r'name="form_token" value="([^"]+)"', r.text).group(1)
r = s.post(f"{BASE}/apply/{token}/", data={
    "form_token": ftok, "lang": "ru", "name": "X", "prefix": "+993",
    "phone": "1", "experience": "", "email": "bad",
}, files={"file": ("virus.exe", io.BytesIO(b"x"), "application/octet-stream")})
check(
    "ошибки валидации",
    r.status_code == 400 and r.text.count('class="field bad"') >= 4,
)
count_after = con.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"]
check("плохая заявка НЕ сохранена", count_after == count_before)

print("== Заявки в панели ==")
r = s.get(BASE + PANEL + "/apps/")
check("заявка в списке", "Мердан Худайгулиев" in r.text and "TV Repair" in r.text)
r = s.get(f"{BASE}{PANEL}/apps/{app_id}/")
check("карточка заявки", "Мердан Худайгулиев" in r.text and "+993 63 12-34-56" in r.text)
r = s.get(f"{BASE}{PANEL}/apps/{app_id}/file/")
check("скачивание файла", r.status_code == 200 and b"%PDF" in r.content)

print("== Смена статуса ==")
r = s.get(f"{BASE}{PANEL}/apps/{app_id}/")
tok = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
r = s.post(f"{BASE}{PANEL}/apps/{app_id}/", data={
    "csrf_token": tok, "action": "status", "status": "contacted"}, allow_redirects=True)
row = con.execute("SELECT status FROM applications WHERE id=?", (app_id,)).fetchone()
check("статус обновлён", row["status"] == "contacted")

print("== Закрытие приёма ==")
r = s.get(BASE + PANEL + "/sites/")
tok = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
sid = re.search(r'name="site_id" value="(\d+)"', r.text).group(1)
r = s.post(BASE + PANEL + "/sites/", data={
    "csrf_token": tok, "site_id": sid, "action": "update",
    "name": "TV Repair by Meryosab", "url": "https://tvrepair.meryosab.com/vacancies/",
    "vacancy": "Мастер по ремонту компьютеров и ноутбуков", "is_closed": "1",
}, allow_redirects=True)
r = s.get(f"{BASE}/apply/{token}/")
check("закрытая анкета отдаёт 410", r.status_code == 410 and "закрыт" in r.text)

print("== Доступ без входа ==")
s2 = requests.Session()
r = s2.get(BASE + PANEL + "/", allow_redirects=False)
check("панель редиректит на логин", r.status_code == 302 and "/login/" in r.headers["Location"])
r = s2.get(f"{BASE}/apply/{token}/")
check("закрытая анкета для всех", r.status_code == 410)

print(f"\nВСЕ ТЕСТЫ ПРОЙДЕНЫ: {ok_count}")
