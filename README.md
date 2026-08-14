# MSB Career — сервис анкет для сайтов вакансий

Отдельный сайт на **Flask** (разворачивается на `msb-career.meryosab.com`).
Принимает анкеты кандидатов со страниц вакансий и отправляет их в **Telegram супер-группу**
(текст + прикреплённый файл резюме). Заявки также сохраняются в базе и видны в админ-панели.

## Как это работает

```
Сайт вакансии (tvrepair.meryosab.com)
   │  кнопка «Заполнить анкету» → https://msb-career.meryosab.com/apply/<ТОКЕН>/
   ▼
Анкета (RU/TM): личные данные, проживание, дата рождения, готовность к работе,
опыт ремонта, навыки, предыдущая работа, желаемая зарплата, документы
   ▼
Сохранение в SQLite  +  отправка в Telegram супер-группу
   ▼
В группе видно: 🌐 Сайт: TV Repair by Meryosab, данные кандидата, файл, № заявки
```

* У **каждого сайта** — своя уникальная ссылка `/apply/<токен>/` (токен генерируется в панели).
* В сообщении Telegram всегда видно, **с какого сайта** прилетела вакансия + хэштег `#заявка #tv-repair-by-meryosab`.
* **Скрытая админ-панель**: добавление сайтов, ссылки, заявки со статусами, настройки бота, смена пароля.

---

## 1. Установка на сервере

```bash
# на сервере, где лежит msb-career.meryosab.com
cd /var/www/msb-career            # папка с этим проектом
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# скопируйте и заполните переменные окружения
cp .env.example .env
nano .env
```

### `.env` — обязательно заполните

Файл `.env` автоматически читается из корня проекта при запуске `python3 wsgi.py`,
`python3 app.py` и через Gunicorn. Переменные, заданные операционной системой,
systemd или Docker, имеют приоритет над значениями из `.env`.

> `ADMIN_LOGIN` и `ADMIN_PASSWORD` используются для **создания первого администратора**.
> Они не заменяют пароль уже существующей учётной записи в `data/career.db`, чтобы пароль,
> изменённый через панель, не сбрасывался при каждом перезапуске.

| Переменная        | Что это |
|-------------------|---------|
| `SECRET_KEY`      | Длинная случайная строка (`openssl rand -hex 32`). Если не задана, один постоянный ключ создаётся в `data/.secret_key`, но для production лучше явно задать свой. |
| `ADMIN_LOGIN`     | Логин админ-панели (по умолчанию `admin`) |
| `ADMIN_PASSWORD`  | Надёжный пароль при первом запуске (минимум 8 символов). Если не задан, приложение сгенерирует безопасный пароль и покажет его в консоли один раз. |
| `PANEL_PATH`      | Секретный путь админ-панели, например `/x-panel-7f3a`. Можно задать своё слово. |
| `DB_PATH`         | Путь к базе SQLite (по умолчанию `data/career.db` внутри проекта) |
| `UPLOAD_DIR`      | Папка загруженных резюме (по умолчанию `uploads/`) |
| `SESSION_COOKIE_SECURE` | Установите `true` на HTTPS-сервере, чтобы cookie панели передавалась только по HTTPS. |

### Запуск через systemd (рекомендуется)

Файл `/etc/systemd/system/msb-career.service`:

```ini
[Unit]
Description=MSB Career (Flask)
After=network.target

[Service]
User=www-data
WorkingDirectory=/var/www/msb-career
EnvironmentFile=/var/www/msb-career/.env
ExecStart=/var/www/msb-career/venv/bin/gunicorn -w 2 -b 127.0.0.1:5000 wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now msb-career
```

### Nginx (пример server-блока)

```nginx
server {
    listen 80;
    server_name msb-career.meryosab.com;

    client_max_body_size 20M;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Не забудьте `certbot` для HTTPS.

---

## 2. Создание Telegram-бота и подключение группы

1. Откройте [@BotFather](https://t.me/BotFather) → `/newbot` → придумайте имя (например `MSB Career Bot`) → **скопируйте токен**.
2. Создайте (или откройте) вашу **супер-группу**, куда должны приходить анкеты.
3. Добавьте бота в группу и сделайте его **администратором** (нужно право отправки сообщений/файлов).
4. Узнайте **Chat ID** супер-группы: он начинается с `-100…`.
   Простой способ: добавьте в группу бота [@getmyid](https://t.me/getmyid) — он пришлёт ID и его можно убрать.
5. В админ-панели откройте **⚙️ Настройки** → вставьте токен и Chat ID. Если в группе включены темы,
   также укажите числовой **ID темы / топика** (`message_thread_id`); для основной темы оставьте поле пустым.
6. Кнопки **«Проверить бота»** и **«Тестовое сообщение в группу»** проверяют значения прямо из полей,
   даже до сохранения. После успешного теста нажмите **«Сохранить»**.

---

## 3. Подключение кнопки на сайте вакансии

1. В админ-панели откройте **🌐 Сайты** → «Добавить сайт»:
   * название: `TV Repair by Meryosab`
   * URL: `https://tvrepair.meryosab.com`
   * вакансия по умолчанию: `Мастер по ремонту компьютеров и ноутбуков`
2. Скопируйте готовую ссылку, например:
   `https://msb-career.meryosab.com/apply/Kj3vQ9xAb12CdEfG/`
3. На странице вакансии `https://tvrepair.meryosab.com/vacancy/computer-repair-technician/`
   замените у кнопки «Заполнить анкету и отправить документы» ссылку
   `https://msb-career.meryosab.com/` на скопированную ссылку с токеном.
4. Готово: анкеты будут приходить в группу с пометкой сайта.

> Для каждого нового сайта-партнёра создавайте отдельную запись в «Сайтах» — так в Telegram
> всегда будет видно источник. Если нужно закрыть приём анкет — нажмите «Закрыть приём» в карточке сайта.

---

## 4. Админ-панель

* Адрес: `https://msb-career.meryosab.com<PANEL_PATH>/login/` (по умолчанию `/x-panel-7f3a/login/`)
* Путь скрыт (`robots.txt` запрещает индексацию), вход защищён паролем и ограничением попыток.
* Возможности: дашборд со статистикой, управление сайтами/токенами, список и карточки заявок,
  статусы (Новая → Связались → Принят / Отклонена), скачивание резюме, повторная отправка в Telegram,
  настройки бота, смена пароля.

## 5. Локальный запуск (для проверки)

```bash
cd msb-career
python3 -m venv venv && venv/bin/pip install -r requirements.txt
ADMIN_PASSWORD=admin123 venv/bin/python wsgi.py   # или: python app.py
# http://localhost:5000 — главная
# http://localhost:5000/x-panel-7f3a/login/ — панель (admin / admin123)
```

Для запуска по локальному HTTP (`localhost` или `192.168.x.x`) укажите в `.env`:

```env
SESSION_COOKIE_SECURE=false
```

Значение `true` применяется только вместе с HTTPS. Иначе браузер не возвращает
сессионную cookie и Flask-WTF отвечает ошибкой `The CSRF session token is missing`.
После изменения `.env` обязательно перезапустите приложение.

## Структура проекта

```
msb-career/
├── app.py              # всё приложение: публичная анкета + админ-панель
├── models.py           # SQLite-модели (sites, applications, admins, settings)
├── wsgi.py             # вход для gunicorn
├── requirements.txt
├── .env.example
├── templates/
│   ├── apply.html      # анкета кандидата (RU/TM, drag&drop файла)
│   ├── success.html    # «Анкета отправлена, № заявки»
│   ├── apply_closed.html / home.html / 404.html
│   └── panel/          # скрытая админ-панель
├── data/career.db      # база (создаётся автоматически)
└── uploads/            # загруженные резюме (создаётся автоматически)
```
