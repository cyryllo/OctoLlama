"""Lista hostów Ollamy widocznych z tego panelu.

hosts.json: [{"nazwa": ..., "ip": ..., "adres": "http://<ip>:11434", "modele_llm": [...]}]
`modele_llm` to modele na TYM hoście, które user świadomie włączył do agregatora
LiteLLM (zakładka "LLM") — domyślnie puste, dopóki ktoś ich nie zaznaczy
(patrz litellm_manager.py, gdzie ta lista filtruje wykryj_wszystkie_modele()).

Pierwszy wpis to zawsze MASTER (ten host, localhost:11434) - auto-tworzony,
niekasowalny stąd (zakładka "Slave" pokazuje tylko resztę, bez niego), bo jego
modele też mają być wybieralne w zakładce "LLM" tak samo jak modele slave'ów.

status.json każdego ZDALNEGO hosta ląduje na tym samym dysku co ten kod (bo
workstation to serwer NFS, a demon zdalnego hosta pisze przez zamontowany
eksport — patrz README.md, "Wielohostowość") pod HOSTS_STATE_BASE/<nazwa>/.
"""

import json
import os
from pathlib import Path

from i18n import przetlumacz as _

HOSTS_PATH = Path(os.environ.get("OCTOLLAMA_HOSTS_FILE", Path(__file__).parent / "hosts.json"))
HOSTS_STATE_BASE = Path(
    os.environ.get("OCTOLLAMA_HOSTS_STATE_BASE", "/srv/octollama/hosts")
)

NAZWA_MASTER = "master"


def wczytaj_hosty():
    try:
        hosty = json.loads(HOSTS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        hosty = []

    if not any(h.get("master") for h in hosty):
        hosty.insert(
            0,
            {
                "nazwa": NAZWA_MASTER,
                "ip": "127.0.0.1",
                "adres": "http://localhost:11434",
                "modele_llm": [],
                "master": True,
            },
        )
        zapisz_hosty(hosty)

    return hosty


def zapisz_hosty(hosty):
    tmp = HOSTS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(hosty, indent=2, ensure_ascii=False))
    tmp.rename(HOSTS_PATH)


def wczytaj_slave_hosty():
    # WHY: zakładka "Slave" zarządza tylko zdalnymi maszynami - master ma
    # swoją własną zakładkę i nie da się go stąd usunąć/edytować jak slave'a.
    return [h for h in wczytaj_hosty() if not h.get("master")]


def znajdz_host(nazwa):
    for h in wczytaj_hosty():
        if h["nazwa"] == nazwa:
            return h
    return None


def dodaj_host(nazwa, ip, mac=None):
    if nazwa == NAZWA_MASTER:
        raise ValueError(
            _("Nazwa '{nazwa}' jest zarezerwowana dla tego hosta.").format(nazwa=NAZWA_MASTER)
        )
    hosty = wczytaj_hosty()
    if any(h["nazwa"] == nazwa for h in hosty):
        raise ValueError(_("Host o nazwie '{nazwa}' już istnieje.").format(nazwa=nazwa))
    hosty.append(
        {"nazwa": nazwa, "ip": ip, "adres": f"http://{ip}:11434", "mac": mac, "modele_llm": []}
    )
    zapisz_hosty(hosty)


def usun_host(nazwa):
    if nazwa == NAZWA_MASTER:
        raise ValueError(_("Nie można usunąć tego hosta (master)."))
    hosty = [h for h in wczytaj_hosty() if h["nazwa"] != nazwa]
    zapisz_hosty(hosty)


def ustaw_modele_llm(nazwa, modele):
    hosty = wczytaj_hosty()
    for h in hosty:
        if h["nazwa"] == nazwa:
            h["modele_llm"] = modele
    zapisz_hosty(hosty)


def ustaw_mac(nazwa, mac):
    hosty = wczytaj_hosty()
    for h in hosty:
        if h["nazwa"] == nazwa:
            h["mac"] = mac
    zapisz_hosty(hosty)


def wczytaj_status_hosta(nazwa):
    try:
        return json.loads((HOSTS_STATE_BASE / nazwa / "status.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _sciezka_state_hosta(nazwa):
    # WHY: master ma stan lokalnie (state_store.py), zdalne hosty mają swój
    # state.json na tym samym dysku co status.json (workstation to serwer NFS
    # dla obu plików - patrz moduł docstring).
    if nazwa == NAZWA_MASTER:
        import state_store

        return state_store.STATE_PATH
    return HOSTS_STATE_BASE / nazwa / "state.json"


def wczytaj_stan_hosta(nazwa):
    # WHY: jak state_store.wczytaj_stan(), ale dla DOWOLNEGO zdalnego hosta -
    # zakładka Slave pozwala edytować zmienne środowiskowe Ollamy per-host, nie
    # tylko dla mastera (ten sam kształt state.json["ollama"], ten sam demon
    # go stosuje niezależnie od hosta, patrz daemon/octollama_daemon.py).
    import state_store

    sciezka = _sciezka_state_hosta(nazwa)
    try:
        return json.loads(sciezka.read_text())
    except (OSError, json.JSONDecodeError):
        stan = json.loads(json.dumps(state_store.DOMYSLNY_STAN))  # deep copy
        # WHY: dopóki ten host nigdy nie dostał żadnego state.json, "domyślny"
        # stan musi odzwierciedlać to, co jego demon już zmierzył - inaczej
        # pierwszy zapis wyzerowałby zmienne środowiskowe sprzed instalacji
        # (ten sam powód co w state_store.wczytaj_stan).
        status = wczytaj_status_hosta(nazwa)
        if status and "ollama" in status:
            stan["ollama"]["env"] = status["ollama"].get("env", {})
            stan["ollama"]["service_running"] = status["ollama"].get("service_running", False)
            stan["ollama"]["service_enabled"] = status["ollama"].get("service_enabled", False)
        return stan


def zapisz_stan_hosta(nazwa, stan):
    sciezka = _sciezka_state_hosta(nazwa)
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    tmp = sciezka.with_suffix(".tmp")
    tmp.write_text(json.dumps(stan, indent=2, ensure_ascii=False))
    tmp.rename(sciezka)


def ustaw_zasilanie(nazwa, akcja):
    # WHY: wyłączenie/restart/uśpienie to operacja uprzywilejowana na TAMTYM
    # hoście - panel WWW nie woła niczego bezpośrednio, tylko zapisuje żądanie
    # do state.json tego hosta; jego lokalny demon je stosuje i SAM kasuje flagę
    # przed wykonaniem (patrz daemon/octollama_daemon.py,
    # zastosuj_zasilanie) - inaczej po wybudzeniu maszyna wyłączyłaby się od razu
    # ponownie, widząc tę samą, nieskasowaną flagę.
    sciezka = _sciezka_state_hosta(nazwa)
    try:
        stan = json.loads(sciezka.read_text())
    except (OSError, json.JSONDecodeError):
        stan = {}
    stan["zasilanie"] = {"akcja": akcja}
    try:
        sciezka.parent.mkdir(parents=True, exist_ok=True)
        tmp = sciezka.with_suffix(".tmp")
        tmp.write_text(json.dumps(stan, indent=2, ensure_ascii=False))
        tmp.rename(sciezka)
    except OSError as e:
        # WHY: katalog tego hosta może być niezapisywalny dla usera panelu (np.
        # świeżo utworzony przez demona z innym właścicielem/uprawnieniami niż
        # 0777 - patrz WHY w daemon/octollama_daemon.py, zastosuj_eksporty_nfs)
        # - to ma się skończyć czytelnym komunikatem, nie gołym 500.
        raise RuntimeError(
            _("Nie udało się zapisać żądania zasilania dla hosta {nazwa}: {blad}").format(
                nazwa=nazwa, blad=e
            )
        )
