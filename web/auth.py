"""Logowanie jeden-użytkownik — hash hasła w lokalnym pliku, bez bazy danych.

Plik poświadczeń tworzy się/aktualizuje przez `python3 manage_users.py`, nie przez
panel WWW — narzędzie osobiste, jeden użytkownik, zero potrzeby na rejestrację.
"""

import json
import os
from pathlib import Path

from werkzeug.security import check_password_hash

CREDENTIALS_PATH = Path(
    os.environ.get("OCTOLLAMA_CREDENTIALS", Path(__file__).parent / "credentials.json")
)


def wczytaj_poswiadczenia():
    try:
        return json.loads(CREDENTIALS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def zweryfikuj(username, password):
    dane = wczytaj_poswiadczenia()
    if not dane:
        return False
    return username == dane.get("username") and check_password_hash(
        dane.get("password_hash", ""), password
    )
