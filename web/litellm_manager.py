"""Sterowanie agregatorem LiteLLM na hoście-gatewayu (tam, gdzie stoi panel WWW).

W przeciwieństwie do usługi Ollama (system, root, przez state.json + demon —
patrz README.md sekcja Architektura), LiteLLM chodzi jako usługa `systemd --user`
i NIE wymaga roota — panel WWW steruje nią BEZPOŚREDNIO, tak samo jak operacjami
na modelach przez `/api/...`. Logika przeniesiona z Ollama Managera
(~/Projekty/Ollama-manager, funkcje `litellm_*`, `_zbuduj_config_litellm`,
`_wykryj_modele_na_serwerach`, `uv_zapewnij`) — działa 1:1, bo już tam była
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
import yaml

import hosts_store
import litellm_ustawienia
from i18n import przetlumacz as _

LITELLM_URL = "http://localhost:4000"
CONFIG_PATH = Path.home() / ".config" / "octollama" / "litellm_config.yaml"
SERVICE_PATH = Path.home() / ".config" / "systemd" / "user" / "litellm.service"

# WHY: marker w model_info (pole na dowolne metadane, LiteLLM go nie rusza)
# żeby przy scalaniu configu odróżnić NASZE wpisy model_list od tych, które
# user dopisał tam ręcznie (patrz _zbuduj_config_dane).
MARKER_KLUCZ = "managed_by"
MARKER_WARTOSC = "octollama"


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


# =============================================================================
#  Instalacja (uv tool install — bez roota, bez systemowego pip, patrz WHY w
#  Ollama Managerze przy uv_zapewnij: Debian PEP 668 blokuje 'pip install --user')
# =============================================================================
def _uv_binarka():
    znaleziona = shutil.which("uv")
    if znaleziona:
        return znaleziona
    kandydat = Path.home() / ".local" / "bin" / "uv"
    return str(kandydat) if kandydat.exists() else None


def uv_zapewnij():
    uv = _uv_binarka()
    if uv:
        return uv
    wynik = subprocess.run(
        ["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
        capture_output=True, text=True, timeout=None,
    )
    if wynik.returncode != 0:
        raise RuntimeError(wynik.stderr.strip() or _("instalacja uv: nieznany błąd"))
    uv = _uv_binarka()
    if not uv:
        raise RuntimeError(_("uv zainstalowane, ale binarki nie znaleziono w ~/.local/bin"))
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
    uv = uv_zapewnij()
    wynik = subprocess.run(
        [uv, "tool", "install", "litellm[proxy]"],
        capture_output=True, text=True, timeout=None,
    )
    if wynik.returncode != 0:
        raise RuntimeError(wynik.stderr.strip() or _("uv tool install: nieznany błąd"))


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
    # to, co już włączone). Capabilities (np. "tools", "insert", "embedding")
    # to info wprost z /api/tags Ollamy - używane do podpowiedzi ról Continue,
    # patrz _role_domyslne.
    wpisy = []
    for h in hosty:
        adres = h["adres"].rstrip("/")
        try:
            r = requests.get(f"{adres}/api/tags", timeout=3)
            r.raise_for_status()
            modele = r.json().get("models", [])
        except requests.RequestException:
            continue  # WHY: host akurat nieosiągalny - pomijamy, nie wywalamy całości
        for m in modele:
            wpisy.append((h["nazwa"], m["name"], adres, tuple(m.get("capabilities", []))))
    return wpisy


def _wykryj_modele_wlaczone(hosty):
    wlaczone = {h["nazwa"]: set(h.get("modele_llm", [])) for h in hosty}
    return [
        (nazwa, model, adres, capabilities)
        for nazwa, model, adres, capabilities in wykryj_wszystkie_modele(hosty)
        if model in wlaczone.get(nazwa, set())
    ]


def modele_capabilities(hosty):
    # WHY: do UI zakładki "LLM" (badge przy modelu) i do domyślnych ról w
    # configu Continue (_role_domyslne) - suma capability zgłaszanych przez
    # WSZYSTKIE hosty, na których dany model jest włączony (w praktyce to
    # ten sam model, więc te same capability, ale unia jest bezpieczna,
    # gdyby akurat dwie instancje Ollamy miały różne wersje).
    wynik = {}
    for _nazwa, model, _adres, capabilities in _wykryj_modele_wlaczone(hosty):
        wynik.setdefault(model, set()).update(capabilities)
    return {model: sorted(caps) for model, caps in wynik.items()}


def _yaml_str(tekst):
    return '"' + tekst.replace("\\", "\\\\").replace('"', '\\"') + '"'


def modele_zbalansowane(hosty):
    # WHY: do UI zakładki "LLM" - pokazujemy, które model_name faktycznie
    # rozkładają ruch na >1 hosta, żeby user widział efekt swojego wyboru
    # checkboxów, zanim jeszcze zapisze/wystartuje LiteLLM.
    wpisy = _wykryj_modele_wlaczone(hosty)
    hosty_per_model = {}
    for nazwa_hosta, model, _adres, _capabilities in wpisy:
        hosty_per_model.setdefault(model, set()).add(nazwa_hosta)
    return {model: sorted(h) for model, h in hosty_per_model.items() if len(h) > 1}


def _czy_nasz_wpis(wpis):
    return isinstance(wpis, dict) and (wpis.get("model_info") or {}).get(MARKER_KLUCZ) == MARKER_WARTOSC


def _wczytaj_istniejacy_config():
    try:
        tresc = CONFIG_PATH.read_text()
    except OSError:
        return {}
    try:
        dane = yaml.safe_load(tresc)
    except yaml.YAMLError:
        return {}  # WHY: plik uszkodzony ręczną edycją - traktujemy jak pusty, nie wywalamy zapisu
    return dane if isinstance(dane, dict) else {}


def _zbuduj_model_list(hosty, ustawienia):
    priorytet = ustawienia.get("priorytet", {})
    lista = []
    for nazwa_hosta, model, adres, _capabilities in _wykryj_modele_wlaczone(hosty):
        litellm_params = {"model": f"ollama_chat/{model}", "api_base": adres}
        # WHY: order dopisujemy TYLKO gdy user jawnie ustawił priorytet dla
        # TEGO hosta w TYM modelu - inaczej zostaje czysty load balancing
        # (patrz CEL pkt 4 - "jeśli nie ustawi kolejności, nie wpisuj order").
        order = priorytet.get(model, {}).get(nazwa_hosta)
        if order is not None:
            litellm_params["order"] = order
        lista.append(
            {
                "model_name": model,
                "litellm_params": litellm_params,
                "model_info": {MARKER_KLUCZ: MARKER_WARTOSC},
            }
        )
    return lista


def _zbuduj_router_settings(ustawienia):
    dane = {
        "routing_strategy": ustawienia.get("routing_strategy", "simple-shuffle"),
        "num_retries": ustawienia.get("num_retries", 2),
        "timeout": ustawienia.get("timeout", 600),
        "cooldown_time": ustawienia.get("cooldown_time", 60),
        "allowed_fails": ustawienia.get("allowed_fails", 3),
    }
    if ustawienia.get("context_window_fallbacks_wlaczone"):
        dane["enable_pre_call_checks"] = True
    return dane


def _zbuduj_fallback_liste(mapowanie):
    # WHY: LiteLLM oczekuje listy jednokluczowych słowników {model: [zapasowy]},
    # nie jednego wspólnego słownika - jeden model teoretycznie mógłby mieć
    # więcej niż jeden zapasowy wpis, stąd lista jako wartość.
    return [{model: [zapasowy]} for model, zapasowy in mapowanie.items() if zapasowy]


def _zbuduj_litellm_settings(ustawienia):
    dane = {}
    fallbacks = _zbuduj_fallback_liste(ustawienia.get("fallbacks", {}))
    if fallbacks:
        dane["fallbacks"] = fallbacks
    if ustawienia.get("context_window_fallbacks_wlaczone"):
        cwf = _zbuduj_fallback_liste(ustawienia.get("context_window_fallbacks", {}))
        if cwf:
            dane["context_window_fallbacks"] = cwf
    return dane


def _zbuduj_config_dane(hosty, ustawienia):
    # WHY: scalanie zamiast nadpisania - user mógł ręcznie dopisać do configu
    # własne wpisy (np. model_list z kluczem API zewnętrznego providera,
    # sekcję general_settings) i nie wolno ich ukraść przy każdym starcie usługi.
    istniejacy = _wczytaj_istniejacy_config()
    scalony = dict(istniejacy)

    obce = [w for w in (istniejacy.get("model_list") or []) if not _czy_nasz_wpis(w)]
    scalony["model_list"] = obce + _zbuduj_model_list(hosty, ustawienia)

    router = dict(istniejacy.get("router_settings") or {})
    router.update(_zbuduj_router_settings(ustawienia))
    scalony["router_settings"] = router

    ls = dict(istniejacy.get("litellm_settings") or {})
    # WHY: kasujemy nasze klucze przed update - jeśli user wyłączył fallback
    # dla wszystkich modeli, _zbuduj_litellm_settings zwróci słownik bez
    # klucza "fallbacks" i to ma realnie skasować stary wpis z poprzedniego
    # zapisu, a nie zostawić go osieroconego.
    ls.pop("fallbacks", None)
    ls.pop("context_window_fallbacks", None)
    ls.update(_zbuduj_litellm_settings(ustawienia))
    if ls:
        scalony["litellm_settings"] = ls
    else:
        scalony.pop("litellm_settings", None)

    return scalony


def zapisz_config(hosty):
    ustawienia = litellm_ustawienia.wczytaj_ustawienia()
    dane = _zbuduj_config_dane(hosty, ustawienia)
    tresc = yaml.safe_dump(dane, sort_keys=False, allow_unicode=True, default_flow_style=False)
    try:
        yaml.safe_load(tresc)  # walidacja przed zapisem - patrz CEL, kryteria akceptacji
    except yaml.YAMLError as e:
        raise RuntimeError(
            _("Wygenerowany config LiteLLM jest niepoprawnym YAML-em: {blad}").format(blad=e)
        )
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(tresc)


def modele_wystawione(hosty):
    # WHY: Continue rozmawia tylko z LiteLLM (jeden endpoint zgodny z OpenAI),
    # więc liczy się TA sama lista, którą LiteLLM faktycznie wystawia pod
    # /v1/models - te same włączone modele co w _zbuduj_config, nie wszystko,
    # co jest zainstalowane. Model może wystąpić na kilku hostach naraz
    # (LiteLLM sam routuje) - Continue widzi tylko nazwę, więc dedupe.
    return sorted({model for _nazwa, model, _adres, _capabilities in _wykryj_modele_wlaczone(hosty)})


# WHY: mapowanie capability zgłaszanych przez Ollamę (/api/tags) na sensowną
# domyślną rolę Continue - "tools" (function-calling) daje edit/apply, "insert"
# (FIM) daje autocomplete, model czysto embeddingowy (bez "completion") dostaje
# TYLKO "embed" zamiast "chat", żeby nie wylądował w selektorze czatu.
def _role_domyslne(capabilities):
    capabilities = set(capabilities)
    if "embedding" in capabilities and "completion" not in capabilities:
        return ["embed"]
    role = ["chat"]
    if "insert" in capabilities:
        role.append("autocomplete")
    if "tools" in capabilities:
        role += ["edit", "apply"]
    return role


def rola_dostepna(rola, capabilities):
    # WHY: brak listy capabilities w ogóle (stara Ollama, która ich nie
    # zgłasza) nie ma nas blokować na ślepo - ograniczamy wybór TYLKO gdy
    # faktycznie wiemy, czego model nie potrafi (patrz ROLA_WYMAGANA_CAPABILITY).
    capabilities = set(capabilities)
    if not capabilities:
        return True
    wymagana = litellm_ustawienia.ROLA_WYMAGANA_CAPABILITY.get(rola)
    return wymagana is None or wymagana in capabilities


def role_dla_modelu(model, capabilities, role_modele):
    # WHY: user mógł ręcznie nadpisać role per model w zakładce LLM
    # (litellm_ustawienia.json, klucz role_modele) - to ma pierwszeństwo
    # przed podpowiedzią z capabilities Ollamy.
    role = role_modele[model] if model in role_modele else _role_domyslne(capabilities)
    # WHY: obrona w głębi - nawet jeśli role_modele ma zapisaną rolę, na którą
    # capability już nie pozwala (np. zapisaną zanim model je stracił, albo z
    # ominięcia niewidocznego w UI checkboxa), taka rola i tak zniknie stąd,
    # zanim trafi do configu Continue.
    return [r for r in role if rola_dostepna(r, capabilities)]


def zbuduj_config_continue(modele, capabilities=None, role_modele=None):
    # WHY: ŻADNYCH kotwic YAML (&anchor / <<: *anchor), każdy model rozpisany
    # osobno - Continue parsuje YAML 1.2, gdzie merge keys bez jawnego
    # nagłówka "%YAML 1.1" po cichu się nie stosują i model znika z selektora
    # klienta (potwierdzone w Ollama Managerze na testach BC-250 + Continue).
    if not modele:
        return "models: []\n"
    capabilities = capabilities or {}
    role_modele = role_modele or {}
    linie = ["name: Local LiteLLM", "version: 1.0.0", "schema: v1", "", "models:"]
    for model in modele:
        linie.append(f"  - name: {_yaml_str(model)}")
        linie.append("    provider: openai")
        linie.append(f"    model: {_yaml_str(model)}")
        linie.append(f"    apiBase: {LITELLM_URL}/v1")
        linie.append("    apiKey: sk-anything")
        linie.append("    roles:")
        for rola in role_dla_modelu(model, capabilities.get(model, ()), role_modele):
            linie.append(f"      - {rola}")
    return "\n".join(linie) + "\n"


def _zapisz_unit():
    bin_ = binarka()
    if not bin_:
        raise RuntimeError(_("LiteLLM nie jest zainstalowane."))
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
    for _proba in range(60):
        if dziala():
            return
        time.sleep(1)
    raise RuntimeError(
        _("LiteLLM nie odpowiedziało w ciągu 60 s. Log usługi: journalctl --user -u litellm -e")
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
