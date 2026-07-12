"""Odczyt/zapis state.json i odczyt status.json — jedyny kanał komunikacji z demonem.

Ta sama ścieżka co OLLAMA_MANAGER_STATE_DIR w daemon/ollama_manager_daemon.py — na
workstation to lokalny katalog, na zdalnych hostach to punkt montowania NFS (patrz
README.md, sekcja "Wielohostowość").
"""

import json
import os
from pathlib import Path

STATE_DIR = Path(os.environ.get("OLLAMA_MANAGER_STATE_DIR", "/var/lib/ollama-manager/state"))
STATE_PATH = STATE_DIR / "state.json"
STATUS_PATH = STATE_DIR / "status.json"

DOMYSLNY_STAN = {
    "ollama": {
        "zainstaluj_ollama": False,
        "service_running": False,
        "service_enabled": False,
        "env": {},
    },
    # WHY: lista {"nazwa", "ip"} zdalnych hostów (zakładka Slave) - demon na
    # workstation na jej podstawie zarządza /etc/exports.d (patrz daemon
    # ollama_manager_daemon.py, zastosuj_eksporty_nfs).
    "nfs_eksporty": [],
}


def wczytaj_stan():
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        stan = json.loads(json.dumps(DOMYSLNY_STAN))  # deep copy
        # WHY: dopóki panel nigdy niczego nie zapisał, "domyślny" stan MUSI
        # odzwierciedlać to, co demon realnie zmierzył (patrz status.json) -
        # inaczej pierwszy zapis JAKIEJKOLWIEK zmiany (nawet samego "Start")
        # wyzerowałby zmienne środowiskowe, które Ollama miała ustawione
        # jeszcze PRZED instalacją tego projektu.
        status = wczytaj_status()
        if status and "ollama" in status:
            stan["ollama"]["env"] = status["ollama"].get("env", {})
            stan["ollama"]["service_running"] = status["ollama"].get("service_running", False)
            stan["ollama"]["service_enabled"] = status["ollama"].get("service_enabled", False)
        return stan


def zapisz_stan(stan):
    # WHY: zapis przez tymczasowy plik + rename = atomowo (IN_MOVED_TO po stronie
    # demona), żeby nigdy nie doszło do odczytu połowicznie zapisanego JSON-a.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(stan, indent=2, ensure_ascii=False))
    tmp.rename(STATE_PATH)


def zsynchronizuj_nfs_eksporty(hosty_slave):
    stan = wczytaj_stan()
    stan["nfs_eksporty"] = [{"nazwa": h["nazwa"], "ip": h["ip"]} for h in hosty_slave]
    zapisz_stan(stan)


def wczytaj_status():
    try:
        return json.loads(STATUS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
