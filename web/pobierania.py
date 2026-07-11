"""Stan pobierania modeli w tle — w pamięci procesu (jeden user, jeden proces
Flask, więc bez potrzeby na Redis/bazę). `/api/pull` strumieniuje minuty do
godzin dla dużych modeli — request HTTP nie może na to czekać, więc pobieranie
leci w osobnym wątku, a strona odpytuje ten moduł o postęp (patrz modele.html,
`<meta http-equiv="refresh">` kiedy coś jeszcze trwa).
"""

import threading

_stan = {}  # (nazwa_hosta, model) -> {"procent", "status", "blad", "gotowe"}
_lock = threading.Lock()


def rozpocznij(nazwa_hosta, model, client):
    klucz = (nazwa_hosta, model)
    with _lock:
        if klucz in _stan and not _stan[klucz]["gotowe"]:
            return  # już trwa - nie duplikuj
        _stan[klucz] = {"procent": None, "status": "rozpoczynanie...", "blad": None, "gotowe": False}

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
        except Exception as e:
            with _lock:
                _stan[klucz]["blad"] = str(e)
                _stan[klucz]["gotowe"] = True

    threading.Thread(target=w_tle, daemon=True).start()


def stan_hosta(nazwa_hosta):
    with _lock:
        return {model: dict(v) for (h, model), v in _stan.items() if h == nazwa_hosta}


def aktywne(nazwa_hosta):
    return any(not v["gotowe"] for v in stan_hosta(nazwa_hosta).values())
