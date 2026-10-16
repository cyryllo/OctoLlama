"""Sterowanie Open WebUI — panelem czatu w przeglądarce (zakładka "LLM").

W przeciwieństwie do Ollama Managera (gdzie Open WebUI łączy się z JEDNYM
wybranym hostem przez OLLAMA_BASE_URL), tutaj WebUI jest podpięte pod LiteLLM
(ten sam endpoint OpenAI-kompatybilny co Continue.dev, patrz
litellm_manager.zbuduj_config_continue) — dzięki temu widzi dokładnie te
modele, które user świadomie wybrał w zakładce LLM, ze wszystkich hostów
naraz, bez własnej logiki wielohostowości. `ENABLE_OLLAMA_API=false` wyłącza
bezpośrednie wykrywanie lokalnej Ollamy, żeby nie omijało tego wyboru.

Usługa systemd --user, bez roota — ten sam wzorzec co LiteLLM
(`litellm_manager.py`, przeniesiony z Ollama Managera).
"""

import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import requests

from i18n import przetlumacz as _
import litellm_manager
from litellm_manager import LITELLM_URL, uv_zapewnij

WEBUI_URL = "http://localhost:8080"
SERVICE_PATH = Path.home() / ".config" / "systemd" / "user" / "open-webui.service"


def _systemctl_user(args):
    r = subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, timeout=10
    )
    if r.returncode != 0:
        raise RuntimeError(
            r.stderr.strip()
            or _("systemctl --user {polecenie}: kod wyjścia {kod}").format(
                polecenie=" ".join(args), kod=r.returncode
            )
        )


def binarka():
    znaleziona = shutil.which("open-webui")
    if znaleziona:
        return znaleziona
    kandydat = Path.home() / ".local" / "bin" / "open-webui"
    return str(kandydat) if kandydat.exists() else None


def zainstalowane():
    return binarka() is not None


def zainstaluj():
    # WHY: Open WebUI (stan na 2026) nie wspiera jeszcze najnowszego Pythona
    # systemowego - 'uv' dociąga kompatybilny Python 3.11 sam, bez apt/roota
    # (ten sam wzorzec co litellm_manager.zainstaluj()).
    uv = uv_zapewnij()
    wynik = subprocess.run(
        [uv, "tool", "install", "--python", "3.11", "open-webui"],
        capture_output=True, text=True, timeout=None,
    )
    if wynik.returncode != 0:
        raise RuntimeError(wynik.stderr.strip() or _("uv tool install: nieznany błąd"))


def dziala():
    try:
        r = requests.get(WEBUI_URL, timeout=1)
        return r.status_code < 500
    except requests.RequestException:
        return False


def autostart_wlaczony():
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-enabled", "open-webui.service"],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip() == "enabled"
    except (OSError, subprocess.TimeoutExpired):
        return False


def _zapisz_unit():
    bin_ = binarka()
    if not bin_:
        raise RuntimeError(_("Open WebUI nie jest zainstalowane."))
    tresc = (
        "[Unit]\n"
        "Description=Open WebUI (podpięte pod LiteLLM)\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={bin_} serve\n"
        f"Environment=OPENAI_API_BASE_URL={LITELLM_URL}/v1\n"
        # WHY: z włączonym kluczem API LiteLLM (zakładka LLM) WebUI musi go
        #      podać, inaczej nie zobaczy żadnego modelu.
        f"Environment=OPENAI_API_KEY={litellm_manager.klucz() or 'sk-anything'}\n"
        "Environment=ENABLE_OLLAMA_API=false\n"
        "Restart=on-failure\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    SERVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERVICE_PATH.write_text(tresc)
    _systemctl_user(["daemon-reload"])


def uruchom():
    if dziala():
        return
    _zapisz_unit()
    _systemctl_user(["start", "open-webui.service"])
    # WHY: pierwsze uruchomienie robi migracje bazy i potrafi ściągnąć domyślny
    # model embeddingowy do RAG - 3 minuty zamiast krótszego limitu jak przy LiteLLM.
    for _proba in range(180):
        if dziala():
            return
        time.sleep(1)
    raise RuntimeError(
        _("WebUI nie odpowiedziało w ciągu 3 minut. Log usługi: journalctl --user -u open-webui -e")
    )


def zatrzymaj():
    _systemctl_user(["stop", "open-webui.service"])


def autostart(wlacz):
    if wlacz:
        _zapisz_unit()
        _systemctl_user(["enable", "--now", "open-webui.service"])
    else:
        _systemctl_user(["disable", "--now", "open-webui.service"])


def _czekaj_az_dziala():
    # WHY: restart Open WebUI trwa kilkadziesiąt sekund (migracje bazy) -
    #      bez czekania strona pokazałaby "zatrzymana" tuż po aktualizacji.
    for _proba in range(180):
        if dziala():
            return
        time.sleep(1)
    raise RuntimeError(
        _("WebUI nie odpowiedziało w ciągu 3 minut. Log usługi: journalctl --user -u open-webui -e")
    )


def wersja():
    return litellm_manager.wersja_uv("open-webui")


def aktualizuj():
    # WHAT: aktualizacja + restart, jeśli usługa działała (jak litellm_manager.aktualizuj).
    # UWAGA: baza Open WebUI (konta, czaty) leży w katalogu pakietu w środowisku
    #        uv (open_webui/data/webui.db) - sprawdzone, że 'uv tool install
    #        open-webui@latest' aktualizuje w miejscu i jej nie rusza.
    dzialalo = dziala()
    litellm_manager.uv_aktualizuj("open-webui", ("--python", "3.11"))
    if dzialalo:
        _systemctl_user(["restart", "open-webui.service"])
        _czekaj_az_dziala()


def _sciezka_bazy():
    # WHY: bez DATA_DIR w unicie Open WebUI trzyma dane w katalogu pakietu
    #      (open_webui/data) wewnątrz środowiska narzędzia uv.
    bazy = sorted(
        (Path.home() / ".local" / "share" / "uv" / "tools" / "open-webui").glob(
            "lib/python*/site-packages/open_webui/data/webui.db"
        )
    )
    return bazy[-1] if bazy else None


def _ustaw_klucz_w_bazie(klucz_api):
    # WHAT: podmienia klucz TYLKO przy połączeniu z LiteLLM w zapisanej
    #       konfiguracji Open WebUI (tabela config: klucz -> wartość JSON).
    # BUG naprawiony: Open WebUI przy PIERWSZYM starcie zapisuje połączenia ze
    #       zmiennych środowiskowych do swojej bazy i potem czyta już tylko ją -
    #       zmiana klucza w unicie nic nie dawała i WebUI traciło wszystkie
    #       modele (sprawdzone na 0.12.2/0.12.3). Inne połączenia, dopisane
    #       przez usera w Panelu administratora, zostają nietknięte.
    baza = _sciezka_bazy()
    if not baza:
        return  # WHY: WebUI jeszcze nigdy nie startowało - wystarczy zmienna w unicie
    with sqlite3.connect(baza, timeout=10) as polaczenie:
        wiersze = dict(polaczenie.execute(
            "SELECT key, value FROM config WHERE key IN ('openai.api_base_urls', 'openai.api_keys')"
        ).fetchall())
        adresy = json.loads(wiersze.get("openai.api_base_urls") or "[]")
        klucze = json.loads(wiersze.get("openai.api_keys") or "[]")
        klucze += [""] * (len(adresy) - len(klucze))
        zmieniono = False
        for i, adres in enumerate(adresy):
            if adres.rstrip("/") == f"{LITELLM_URL}/v1" and klucze[i] != klucz_api:
                klucze[i] = klucz_api
                zmieniono = True
        if zmieniono:
            polaczenie.execute(
                "UPDATE config SET value = ?, updated_at = ? WHERE key = 'openai.api_keys'",
                (json.dumps(klucze), int(time.time())),
            )


def zastosuj_klucz():
    # WHAT: nowy klucz API LiteLLM -> unit (pierwszy start WebUI) i zapisana
    #       konfiguracja Open WebUI (każdy kolejny), potem restart działającego.
    if not zainstalowane():
        return
    dzialalo = dziala()
    _zapisz_unit()
    if dzialalo:
        _systemctl_user(["stop", "open-webui.service"])
    try:
        _ustaw_klucz_w_bazie(litellm_manager.klucz() or "sk-anything")
    except (sqlite3.Error, ValueError) as e:
        raise RuntimeError(
            _("Nie udało się zapisać klucza w konfiguracji Open WebUI ({blad}) — wpisz go ręcznie w Panelu administratora → Ustawienia → Połączenia.").format(blad=e)
        )
    finally:
        if dzialalo:
            _systemctl_user(["start", "open-webui.service"])
