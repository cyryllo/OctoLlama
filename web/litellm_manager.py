"""Sterowanie agregatorem LiteLLM na hoście-gatewayu (tam, gdzie stoi panel WWW).

W przeciwieństwie do usługi Ollama (system, root, przez state.json + demon —
patrz README.md sekcja Architektura), LiteLLM chodzi jako usługa `systemd --user`
i NIE wymaga roota — panel WWW steruje nią BEZPOŚREDNIO, tak samo jak operacjami
na modelach przez `/api/...`. Logika przeniesiona z Ollama Managera
(~/Projekty/Ollama-manager, funkcje `litellm_*`, `_zbuduj_config_litellm`,
`_wykryj_modele_na_serwerach`, `_uv_zapewnij`) — działa 1:1, bo już tam była
odseparowana od GUI.

LiteLLM agreguje TYLKO modele, które user świadomie zaznaczył per host w
zakładce "LLM" (hosts_store.py, pole `modele_llm`) — nie wszystko, co jest akurat
zainstalowane, żeby dodanie nowego modelu na hoście nie wystawiało go od razu
na zewnątrz bez decyzji użytkownika.
"""

import shutil
import subprocess
import time
from pathlib import Path

import requests

import hosts_store

LITELLM_URL = "http://localhost:4000"
CONFIG_PATH = Path.home() / ".config" / "ollama-manager" / "litellm_config.yaml"
SERVICE_PATH = Path.home() / ".config" / "systemd" / "user" / "litellm.service"


def _systemctl_user(args):
    r = subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, timeout=10
    )
    if r.returncode != 0:
        raise RuntimeError(
            r.stderr.strip() or f"systemctl --user {' '.join(args)}: kod wyjścia {r.returncode}"
        )


# =============================================================================
#  Instalacja (uv tool install — bez roota, bez systemowego pip, patrz WHY w
#  Ollama Managerze przy _uv_zapewnij: Debian PEP 668 blokuje 'pip install --user')
# =============================================================================
def _uv_binarka():
    znaleziona = shutil.which("uv")
    if znaleziona:
        return znaleziona
    kandydat = Path.home() / ".local" / "bin" / "uv"
    return str(kandydat) if kandydat.exists() else None


def _uv_zapewnij():
    uv = _uv_binarka()
    if uv:
        return uv
    wynik = subprocess.run(
        ["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
        capture_output=True, text=True, timeout=None,
    )
    if wynik.returncode != 0:
        raise RuntimeError(wynik.stderr.strip() or "instalacja uv: nieznany błąd")
    uv = _uv_binarka()
    if not uv:
        raise RuntimeError("uv zainstalowane, ale binarki nie znaleziono w ~/.local/bin")
    return uv


def binarka():
    znaleziona = shutil.which("litellm")
    if znaleziona:
        return znaleziona
    kandydat = Path.home() / ".local" / "bin" / "litellm"
    return str(kandydat) if kandydat.exists() else None


def zainstalowane():
    return binarka() is not None


def zainstaluj():
    uv = _uv_zapewnij()
    wynik = subprocess.run(
        [uv, "tool", "install", "litellm[proxy]"],
        capture_output=True, text=True, timeout=None,
    )
    if wynik.returncode != 0:
        raise RuntimeError(wynik.stderr.strip() or "uv tool install: nieznany błąd")


def dziala():
    try:
        r = requests.get(f"{LITELLM_URL}/health/liveliness", timeout=1)
        return r.status_code < 500
    except requests.RequestException:
        return False


def autostart_wlaczony():
    # WHY: samo wyświetlenie dashboardu nie może się wywalić, jeśli systemd
    # --user akurat nie odpowiada (np. brak sesji D-Bus) - tak samo defensywnie
    # jak _systemctl_query w Ollama Managerze.
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-enabled", "litellm.service"],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip() == "enabled"
    except (OSError, subprocess.TimeoutExpired):
        return False


# =============================================================================
#  Config + unit systemd --user, generowane na nowo przy każdym starcie (lista
#  hostów/modeli mogła się zmienić od ostatniego uruchomienia)
# =============================================================================
def wykryj_wszystkie_modele(hosty):
    # WHY: publiczne, BEZ filtrowania wyboru LLM - do budowy siatki checkboxów
    # w zakładce "LLM" (trzeba pokazać wszystko, co jest do wyboru, nie tylko
    # to, co już włączone).
    wpisy = []
    for h in hosty:
        adres = h["adres"].rstrip("/")
        try:
            r = requests.get(f"{adres}/api/tags", timeout=3)
            r.raise_for_status()
            modele = [m["name"] for m in r.json().get("models", [])]
        except requests.RequestException:
            continue  # WHY: host akurat nieosiągalny - pomijamy, nie wywalamy całości
        for model in modele:
            wpisy.append((h["nazwa"], model, adres))
    return wpisy


def _wykryj_modele_wlaczone(hosty):
    wlaczone = {h["nazwa"]: set(h.get("modele_llm", [])) for h in hosty}
    return [
        (nazwa, model, adres)
        for nazwa, model, adres in wykryj_wszystkie_modele(hosty)
        if model in wlaczone.get(nazwa, set())
    ]


def _yaml_str(tekst):
    return '"' + tekst.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _zbuduj_config(hosty):
    wpisy = _wykryj_modele_wlaczone(hosty)
    if not wpisy:
        return "model_list: []\n"
    linie = ["model_list:"]
    for _nazwa_hosta, model, adres in wpisy:
        linie.append(f"  - model_name: {_yaml_str(model)}")
        linie.append("    litellm_params:")
        linie.append(f"      model: {_yaml_str('ollama_chat/' + model)}")
        linie.append(f"      api_base: {_yaml_str(adres)}")
    return "\n".join(linie) + "\n"


def zapisz_config(hosty):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(_zbuduj_config(hosty))


def modele_wystawione(hosty):
    # WHY: Continue rozmawia tylko z LiteLLM (jeden endpoint zgodny z OpenAI),
    # więc liczy się TA sama lista, którą LiteLLM faktycznie wystawia pod
    # /v1/models - te same włączone modele co w _zbuduj_config, nie wszystko,
    # co jest zainstalowane. Model może wystąpić na kilku hostach naraz
    # (LiteLLM sam routuje) - Continue widzi tylko nazwę, więc dedupe.
    return sorted({model for _nazwa, model, _adres in _wykryj_modele_wlaczone(hosty)})


def zbuduj_config_continue(modele):
    # WHY: ŻADNYCH kotwic YAML (&anchor / <<: *anchor), każdy model rozpisany
    # osobno - Continue parsuje YAML 1.2, gdzie merge keys bez jawnego
    # nagłówka "%YAML 1.1" po cichu się nie stosują i model znika z selektora
    # klienta (potwierdzone w Ollama Managerze na testach BC-250 + Continue).
    if not modele:
        return "models: []\n"
    linie = ["name: Local LiteLLM", "version: 1.0.0", "schema: v1", "", "models:"]
    for model in modele:
        linie.append(f"  - name: {_yaml_str(model)}")
        linie.append("    provider: openai")
        linie.append(f"    model: {_yaml_str(model)}")
        linie.append(f"    apiBase: {LITELLM_URL}/v1")
        linie.append("    apiKey: sk-anything")
        linie.append("    roles:")
        linie.append("      - chat")
        linie.append("      - edit")
        linie.append("      - apply")
    return "\n".join(linie) + "\n"


def _zapisz_unit():
    bin_ = binarka()
    if not bin_:
        raise RuntimeError("LiteLLM nie jest zainstalowane.")
    tresc = (
        "[Unit]\n"
        "Description=LiteLLM - agregator modeli Ollama (gateway)\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={bin_} --config {CONFIG_PATH}\n"
        "Restart=on-failure\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    SERVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERVICE_PATH.write_text(tresc)
    _systemctl_user(["daemon-reload"])


def uruchom():
    hosty = hosts_store.wczytaj_hosty()
    zapisz_config(hosty)
    _zapisz_unit()
    _systemctl_user(["restart", "litellm.service"])
    for _ in range(60):
        if dziala():
            return
        time.sleep(1)
    raise RuntimeError(
        "LiteLLM nie odpowiedziało w ciągu 60 s. Log usługi: journalctl --user -u litellm -e"
    )


def zatrzymaj():
    _systemctl_user(["stop", "litellm.service"])


def autostart(wlacz):
    # WHY: 'enable --now' potrzebuje świeżego configu i unitu - tak samo jak
    # ręczne uruchomienie, żeby autostart nie odpalił się na nieaktualnej liście.
    if wlacz:
        hosty = hosts_store.wczytaj_hosty()
        zapisz_config(hosty)
        _zapisz_unit()
        _systemctl_user(["enable", "--now", "litellm.service"])
    else:
        _systemctl_user(["disable", "--now", "litellm.service"])
