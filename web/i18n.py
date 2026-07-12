"""i18n — słownik tłumaczeń kluczowany polskim tekstem źródłowym, ten sam
wzorzec co Ollama Manager (`lang/*.json` + funkcja `_()`, patrz
~/Projekty/Ollama-manager/ollama_manager.py). Brak wpisu w słowniku = pokazujemy
oryginał (polski), więc niekompletne tłumaczenie nigdy nie pokazuje pustego
pola. Wybrany język trzymany w sesji (ciasteczko), jak reszta stanu logowania.

Dodanie kolejnego języka to nowy plik `lang/<kod>.json` + wpis w JEZYKI, bez
dotykania reszty kodu.
"""

import json
from pathlib import Path

from flask import session

JEZYKI = {"pl": "polski", "en": "English"}
JEZYK_DOMYSLNY = "pl"
_KATALOG_LANG = Path(__file__).parent / "lang"
_cache = {}


def _wczytaj_tlumaczenia(kod):
    if kod == JEZYK_DOMYSLNY:
        return {}  # WHY: polski to sam tekst źródłowy - nie potrzeba pliku
    if kod not in _cache:
        try:
            tresc = (_KATALOG_LANG / f"{kod}.json").read_text(encoding="utf-8")
            _cache[kod] = json.loads(tresc)
        except (OSError, json.JSONDecodeError):
            _cache[kod] = {}
    return _cache[kod]


def aktualny_jezyk():
    kod = session.get("jezyk", JEZYK_DOMYSLNY)
    return kod if kod in JEZYKI else JEZYK_DOMYSLNY


def przetlumacz(tekst):
    return _wczytaj_tlumaczenia(aktualny_jezyk()).get(tekst, tekst)
