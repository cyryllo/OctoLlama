"""Stan pobierania modeli w tle — w pamięci procesu (jeden user, jeden proces
Flask, więc bez potrzeby na Redis/bazę). `/api/pull` strumieniuje minuty do
godzin dla dużych modeli — request HTTP nie może na to czekać, więc pobieranie
leci w osobnym wątku, a strona odpytuje ten moduł o postęp (patrz modele.html,
`<meta http-equiv="refresh">` kiedy coś jeszcze trwa).
"""

import threading
import time

_stan = {}  # (nazwa_hosta, model) -> {"procent", "status", "blad", "gotowe", "zakonczono_o"}
_lock = threading.Lock()

# WHY: po tylu sekundach od zakończenia (sukces albo błąd) wpis znika z listy
# sam - inaczej wisiałby tam na 100% w nieskończoność, aż do restartu procesu.
CZAS_ZNIKANIA_S = 5


def rozpocznij(nazwa_hosta, model, client):
    klucz = (nazwa_hosta, model)
    with _lock:
        if klucz in _stan and not _stan[klucz]["gotowe"]:
            return  # już trwa - nie duplikuj
        _stan[klucz] = {
            "procent": None,
            "status": "rozpoczynanie...",
            "blad": None,
            "gotowe": False,
            "zakonczono_o": None,
        }

    def w_tle():
        try:
            for wpis in client.pull_stream(model):
                procent = None
                if wpis.get("total") and wpis.get("completed"):
                    procent = round(wpis["completed"] / wpis["total"] * 100)
                with _lock:
                    _stan[klucz]["status"] = wpis.get("status", "")
                    _stan[klucz]["procent"] = procent
            with _lock:
                _stan[klucz]["status"] = "gotowe"
                _stan[klucz]["procent"] = 100
                _stan[klucz]["gotowe"] = True
                _stan[klucz]["zakonczono_o"] = time.time()
        except Exception as e:
            with _lock:
                _stan[klucz]["blad"] = str(e)
                _stan[klucz]["gotowe"] = True
                _stan[klucz]["zakonczono_o"] = time.time()

    threading.Thread(target=w_tle, daemon=True).start()


def _wyczysc_stare():
    granica = time.time() - CZAS_ZNIKANIA_S
    for klucz in [
        k for k, v in _stan.items()
        if v["gotowe"] and v["zakonczono_o"] is not None and v["zakonczono_o"] < granica
    ]:
        del _stan[klucz]


def stan_hosta(nazwa_hosta):
    with _lock:
        _wyczysc_stare()
        return {model: dict(v) for (h, model), v in _stan.items() if h == nazwa_hosta}


def aktywne(nazwa_hosta):
    # WHY: stan_hosta() wyżej już wyczyścił wpisy starsze niż CZAS_ZNIKANIA_S,
    # więc cokolwiek zostało - trwające pobieranie albo świeżo zakończone -
    # ma prawo utrzymać auto-odświeżanie strony (patrz modele.html).
    return bool(stan_hosta(nazwa_hosta))
