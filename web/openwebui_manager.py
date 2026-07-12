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

import shutil
import subprocess
import time
from pathlib import Path

import requests

from i18n import przetlumacz as _
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
        "Environment=OPENAI_API_KEY=sk-anything\n"
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
