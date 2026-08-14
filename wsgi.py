# Точка входа для gunicorn: gunicorn -w 2 -b 127.0.0.1:5000 wsgi:app
import os

from app import app
from models import init_db, seed_default_admin

init_db()
with app.app_context():
    seed_default_admin()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
