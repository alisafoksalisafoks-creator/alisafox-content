"""Читает ключи из файла .env в корне проекта, если он есть. Уже заданные переменные не трогает."""
import os
from pathlib import Path

path = Path(__file__).resolve().parent.parent / ".env"
if path.exists():
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))
