"""Sprawdzanie i instalowanie aktualizacji usług: Ollama, LiteLLM, Open WebUI.

Najnowsze wersje: Ollama z GitHuba (oficjalne repo, ten sam adres co release
notes na ollama.com/download), LiteLLM i Open WebUI z PyPI (stamtąd i tak
instaluje uv). Logika przeniesiona z Ollama Managera
(`*_wersja_najnowsza`, `_rozbij_wersje`).

Aktualizacja LiteLLM/Open WebUI to zwykły proces usera (`uv tool install …@latest`),
ale trwa minuty - leci w osobnym wątku, tak samo jak pobieranie modeli
(pobierania.py), a strona pokazuje jej stan. Ollamę aktualizuje demon z rootem
(patrz app._zastosuj_akcje_ollama, akcja "aktualizuj_ollama").
"""

import re
import threading
import time

import requests

ZRODLA = {
    "ollama": ("github", "ollama/ollama"),
    "litellm": ("pypi", "litellm"),
    "open-webui": ("pypi", "open-webui"),
}

# WHY: GitHub bez tokena pozwala na 60 zapytań/h z jednego IP - wynik trzymamy
#      godzinę i pytamy tylko po kliknięciu "Sprawdź aktualizacje".
CZAS_PAMIECI_S = 3600

_najnowsze = {}  # usluga -> (czas_sprawdzenia, wersja)
_zadania = {}    # usluga -> {"trwa", "blad", "zakonczono_o"}
_lock = threading.Lock()


def rozbij_wersje(tekst):
    # WHAT: "0.3.12" -> (0, 3, 12). WHY: porównanie tekstowe źle zestawi
    #       "0.3.9" z "0.3.10". Zatrzymuje się na pierwszym nienumerycznym
    #       segmencie (np. "-rc1"), zamiast wywalić wyjątek.
    wynik = []
    for czesc in re.split(r"[.\-]", (tekst or "").lstrip("v")):
        if not czesc.isdigit():
            break
        wynik.append(int(czesc))
    return tuple(wynik)


def sprawdz_najnowsza(usluga):
    # WHAT: pyta źródło o najnowszą wersję i zapamiętuje wynik. Rzuca
    #       requests.RequestException przy błędzie sieci.
    rodzaj, nazwa = ZRODLA[usluga]
    if rodzaj == "github":
        r = requests.get(
            f"https://api.github.com/repos/{nazwa}/releases/latest",
            timeout=5, headers={"Accept": "application/vnd.github+json"},
        )
        r.raise_for_status()
        wersja = r.json().get("tag_name", "").lstrip("v") or None
    else:
        r = requests.get(f"https://pypi.org/pypi/{nazwa}/json", timeout=5)
        r.raise_for_status()
        wersja = r.json().get("info", {}).get("version") or None
    with _lock:
        _najnowsze[usluga] = (time.time(), wersja)
    return wersja


def najnowsza(usluga):
    # WHAT: zapamiętana najnowsza wersja albo None, gdy jeszcze nie sprawdzano
    #       (albo minęła godzina) - bez zapytania do sieci przy każdym widoku.
    with _lock:
        wpis = _najnowsze.get(usluga)
    if not wpis or time.time() - wpis[0] > CZAS_PAMIECI_S:
        return None
    return wpis[1]


def informacja(usluga, lokalna):
    # WHAT: wszystko, czego szablon potrzebuje do wiersza "Wersja" usługi.
    nowa = najnowsza(usluga)
    return {
        "usluga": usluga,
        "lokalna": lokalna,
        "najnowsza": nowa,
        "dostepna": bool(lokalna and nowa and rozbij_wersje(nowa) > rozbij_wersje(lokalna)),
        "zadanie": zadanie(usluga),
    }


def rozpocznij(usluga, funkcja, funkcja_wersji):
    # WHAT: uruchamia funkcję aktualizacji w tle (jedna naraz na usługę), a po
    #       niej sprawdza, czy zainstalowana wersja to faktycznie najnowsza -
    #       "Zaktualizowano." bez zmiany wersji tylko by mylił.
    oczekiwana = najnowsza(usluga)
    with _lock:
        if _zadania.get(usluga, {}).get("trwa"):
            return
        _zadania[usluga] = {"trwa": True, "blad": None, "zakonczono_o": None}

    def w_tle():
        blad = None
        try:
            funkcja()
            teraz = funkcja_wersji()
            if oczekiwana and teraz != oczekiwana:
                blad = f"po aktualizacji zainstalowana jest {teraz}, a najnowsza to {oczekiwana}"
        except Exception as e:  # WHY: błąd ma trafić na stronę, nie zabić wątku po cichu
            blad = str(e)
        with _lock:
            _zadania[usluga] = {"trwa": False, "blad": blad, "zakonczono_o": time.time()}

    threading.Thread(target=w_tle, daemon=True).start()


def zadanie(usluga):
    # WHY: wynik zakończonej aktualizacji pokazujemy jeszcze przez minutę -
    #      dość, żeby strona po odświeżeniu go wyświetliła, potem znika.
    with _lock:
        stan = _zadania.get(usluga)
        if stan and not stan["trwa"] and time.time() - stan["zakonczono_o"] > 60:
            del _zadania[usluga]
            return None
        return dict(stan) if stan else None
