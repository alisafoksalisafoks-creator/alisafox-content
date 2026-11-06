"""Создаёт API-ключ YouGile и сразу пишет его в /opt/yougile-bot/.env. Ключ на экран не выводится.

Запуск на сервере: /opt/yougile-bot/.venv/bin/python /opt/yougile-bot/yougile_bot/deploy/make_key.py
Логин и пароль — администратора компании в YouGile; они никуда не сохраняются.
"""
import getpass
import json
import re
import sys
import urllib.request
from pathlib import Path

BASE = "https://ru.yougile.com/api-v2"
ENV = Path("/opt/yougile-bot/.env")


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


login = input("Логин (email) администратора YouGile: ").strip()
password = getpass.getpass("Пароль (не отображается): ")
companies = post("/auth/companies", {"login": login, "password": password}).get("content", [])
if not companies:
    sys.exit("Компаний не найдено: проверьте логин и пароль.")
for i, c in enumerate(companies, 1):
    print(f"{i}. {c.get('name')}")
pick = companies[0] if len(companies) == 1 else companies[int(input("Номер компании «ИП Попова»: ")) - 1]
key = post("/auth/keys", {"login": login, "password": password, "companyId": pick["id"]})["key"]
text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
text = (re.sub(r"^YOUGILE_API_KEY=.*$", f"YOUGILE_API_KEY={key}", text, flags=re.M)
        if re.search(r"^YOUGILE_API_KEY=", text, flags=re.M) else text + f"\nYOUGILE_API_KEY={key}\n")
ENV.write_text(text, encoding="utf-8")
ENV.chmod(0o600)
print(f"Готово: ключ для «{pick.get('name')}» записан в {ENV}.")
