#!/usr/bin/env python3
"""CLI do ustawienia/zmiany jedynego użytkownika panelu WWW.

Uruchom: python3 manage_users.py
Zapisuje username + hash hasła do credentials.json (patrz auth.py) — panel WWW
sam nigdy nie zapisuje ani nie zna hasła w postaci jawnej.
"""

import getpass
import json

from auth import CREDENTIALS_PATH
from werkzeug.security import generate_password_hash


def main():
    username = input("Nazwa użytkownika: ").strip()
    if not username:
        print("Nazwa użytkownika nie może być pusta.")
        raise SystemExit(1)

    password = getpass.getpass("Hasło: ")
    powtorzone = getpass.getpass("Powtórz hasło: ")
    if not password:
        print("Hasło nie może być puste.")
        raise SystemExit(1)
    if password != powtorzone:
        print("Hasła się nie zgadzają.")
        raise SystemExit(1)

    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_text(
        json.dumps({"username": username, "password_hash": generate_password_hash(password)}, indent=2)
    )
    CREDENTIALS_PATH.chmod(0o600)
    print(f"Zapisano poświadczenia do {CREDENTIALS_PATH}")


if __name__ == "__main__":
    main()
