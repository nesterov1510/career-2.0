#!/usr/bin/env bash
# Установка сервиса msb-career: права на каталог проекта, юнит systemd, перезапуск.
# Запускать от root из корня репозитория:
#   sudo bash deploy/install.sh
# Скрипт идемпотентен: права выставляются заново, юнит перезаписывается,
# сервис включается в автозагрузку и перезапускается.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$APP_DIR/deploy/msb-career.service"
UNIT_DST="/etc/systemd/system/msb-career.service"
UNIT_DROPIN_DIR="/etc/systemd/system/msb-career.service.d"

if [ "$(id -u)" -ne 0 ]; then
    echo "Ошибка: запустите от root: sudo bash deploy/install.sh" >&2
    exit 1
fi

# Берём User= и WorkingDirectory= из самого юнита, чтобы не разъехаться.
SERVICE_USER="$(awk -F= '/^User=/{print $2}' "$UNIT_SRC")"
UNIT_WORKDIR="$(awk -F= '/^WorkingDirectory=/{print $2}' "$UNIT_SRC")"

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "Ошибка: пользователь $SERVICE_USER не найден в системе" >&2
    exit 1
fi

if [ "$APP_DIR" != "$UNIT_WORKDIR" ]; then
    echo "Внимание: юнит ожидает WorkingDirectory=$UNIT_WORKDIR," >&2
    echo "а скрипт запущен из $APP_DIR. Убедитесь, что пути совпадают." >&2
fi

echo "== Права на каталог проекта ($APP_DIR) =="
# Сервис работает от имени $SERVICE_USER и chdir'ится в $APP_DIR,
# поэтому каждый компонент пути должен быть доступен этому пользователю.
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chmod 750 "$APP_DIR"
# Каталоги, куда приложение пишет (SQLite-база и загруженные резюме).
mkdir -p "$APP_DIR/data" "$APP_DIR/uploads"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/data" "$APP_DIR/uploads"
if [ -f "$APP_DIR/.env" ]; then
    chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
fi

echo "== Юнит systemd =="
if [ -d "$UNIT_DROPIN_DIR" ]; then
    echo "Внимание: найден drop-in каталог $UNIT_DROPIN_DIR —" >&2
    echo "переопределения в нём могут мешать (например, ProtectHome=yes)." >&2
    ls -la "$UNIT_DROPIN_DIR" >&2
fi
install -m 644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload

echo "== Включение и перезапуск =="
systemctl enable msb-career
systemctl restart msb-career
sleep 2

echo "== Проверка =="
BIND="$(awk '{for (i=1; i<NF; i++) if ($i=="--bind") print $(i+1)}' "$UNIT_SRC")"
PORT="${BIND##*:}"
systemctl is-active msb-career || echo "Внимание: сервис не в active!" >&2
if command -v ss >/dev/null 2>&1; then
    if ss -tln | grep -qE "[:.]$PORT\b"; then
        echo "Порт $PORT слушается."
    else
        echo "Внимание: порт $PORT не слушается. Смотрите: sudo journalctl -u msb-career -n 50" >&2
    fi
fi
if command -v curl >/dev/null 2>&1; then
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/" || true)"
    echo "Главная страница отвечает кодом: ${code:-нет ответа}"
fi

systemctl status msb-career --no-pager -l || true
echo
echo "Готово. Логи: sudo journalctl -u msb-career -f"
