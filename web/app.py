#!/usr/bin/env python3
"""Panel WWW ollama-manager — cztery zakładki: Master / Slave / LLM / WebUI.

Ten proces NIGDY nie woła systemctl/pkexec dla usługi Ollama bezpośrednio —
tylko zapisuje docelowy stan do state.json, który stosuje lokalny
ollama-manager-daemon (patrz README.md, sekcja Architektura). LiteLLM/WebUI
(usługi systemd --user, bez roota) i operacje na modelach są sterowane
bezpośrednio.

- Master: usługa Ollama i jej zmienne środowiskowe NA TYM hoście + status
  wszystkich podłączonych hostów (master + slave'y).
- Slave: dodawanie/usuwanie zdalnych hostów Ollamy, pobranie instalatora dla
  każdego z nich.
- LLM: sterowanie agregatorem LiteLLM + wybór, które modele z których hostów
  ma wystawiać.
- WebUI: Open WebUI podpięte pod LiteLLM (widzi te same, świadomie wybrane
  modele, ze wszystkich hostów naraz).
- Modele (/modele/<nazwa>, linkowane z Master/Slave): lista/pobierz/usuń modele
  na DOWOLNYM hoście, bezpośrednio przez /api/... (zero roota, jak LiteLLM).
"""

import ipaddress
import os
import re
import secrets
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, Response, flash, redirect, render_template, request, session, url_for

import hosts_store
import i18n
import install_generator
import litellm_manager as litellm
import litellm_ustawienia
import openwebui_manager as webui
import pobierania
import wol
from auth import zweryfikuj
from i18n import przetlumacz as _
from ollama_client import OllamaClient
from state_store import wczytaj_stan, wczytaj_status, zapisz_stan, zsynchronizuj_nfs_eksporty

app = Flask(__name__)
app.jinja_env.globals["_"] = _
app.jinja_env.globals["JEZYKI"] = i18n.JEZYKI
app.jinja_env.globals["_jezyk_aktualny"] = i18n.aktualny_jezyk


# =============================================================================
#  Auto-odświeżanie stron dopóki demon nie zastosuje zmiany zapisanej przez
#  panel (patrz NOTATKI.md, "Auto-odświeżanie po zmianie ustawień") - per
#  host, bo Slave pokazuje wiele hostów naraz, nie jeden jak Master.
# =============================================================================
def oznacz_oczekiwanie(nazwa_hosta):
    oczekiwania = session.get("oczekiwania", {})
    oczekiwania[nazwa_hosta] = datetime.now(timezone.utc).isoformat()
    session["oczekiwania"] = oczekiwania


def czy_odswiezac(nazwa_hosta, status):
    # WHY: porównanie stringów ISO 8601 UTC (ten sam format co po stronie
    # demona) sortuje się poprawnie jak daty - nie trzeba parsować do datetime
    # przy każdym porównaniu, tylko przy liczeniu wieku dla limitu 30s.
    oczekiwania = session.get("oczekiwania", {})
    oczekuje_od = oczekiwania.get(nazwa_hosta)
    if not oczekuje_od:
        return False

    wiek = (datetime.now(timezone.utc) - datetime.fromisoformat(oczekuje_od)).total_seconds()
    zdazyl = status and status.get("timestamp", "") >= oczekuje_od
    # WHY: po 30s bez odpowiedzi demona (usługa stoi? maszyna się wyłączyła?)
    # przestajemy odświeżać, żeby nie kręcić się w nieskończoność.
    if zdazyl or wiek > 30:
        oczekiwania.pop(nazwa_hosta, None)
        session["oczekiwania"] = oczekiwania
        return False
    return True


def _wczytaj_wersje():
    # WHY: install.sh kopiuje VERSION z korzenia repo do TEGO katalogu
    # (patrz install.sh, krok [6/6]) - stąd szukamy najpierw obok siebie, a
    # przy uruchomieniu wprost z repo (development) - jeden katalog wyżej.
    for kandydat in (Path(__file__).parent / "VERSION", Path(__file__).parent.parent / "VERSION"):
        if kandydat.exists():
            return kandydat.read_text().strip()
    return "?"


app.jinja_env.globals["WERSJA"] = _wczytaj_wersje()


def formatuj_czas(iso_tekst):
    # WHY: status.json trzyma czas w ISO 8601 UTC z mikrosekundami
    # ("2026-07-12T10:02:32.106056+00:00") - dobre do zapisu, nieczytelne do
    # wyświetlenia. Pokazujemy w czasie lokalnym serwera, bez mikrosekund, a
    # dla dzisiejszej daty samą godzinę ze słowem "dziś" zamiast pełnej daty.
    if not iso_tekst:
        return iso_tekst
    try:
        dt = datetime.fromisoformat(iso_tekst).astimezone()
    except ValueError:
        return iso_tekst
    if dt.date() == datetime.now().astimezone().date():
        return f"{_('dziś')}, {dt.strftime('%H:%M:%S')}"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


app.jinja_env.filters["formatuj_czas"] = formatuj_czas

SECRET_KEY_PATH = Path(
    os.environ.get("OLLAMA_MANAGER_SECRET_KEY_FILE", Path(__file__).parent / ".secret_key")
)
if os.environ.get("SECRET_KEY"):
    app.secret_key = os.environ["SECRET_KEY"]
else:
    # WHY: klucz trwały na dysku, żeby restart procesu nie wylogowywał od razu
    # wszystkich sesji — narzędzie osobiste, jeden użytkownik, plik wystarczy.
    if not SECRET_KEY_PATH.exists():
        SECRET_KEY_PATH.write_text(secrets.token_hex(32))
        SECRET_KEY_PATH.chmod(0o600)
    app.secret_key = SECRET_KEY_PATH.read_text().strip()

NAZWA_HOSTA_WZORZEC = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]*$")


def login_required(widok):
    @wraps(widok)
    def opakowany(*args, **kwargs):
        if not session.get("zalogowany"):
            return redirect(url_for("login"))
        return widok(*args, **kwargs)

    return opakowany


@app.route("/login", methods=["GET", "POST"])
def login():
    blad = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if zweryfikuj(username, password):
            jezyk = session.get("jezyk")  # WHY: session.clear() nie ma wymazać wyboru języka
            session.clear()
            session["zalogowany"] = True
            session["username"] = username
            if jezyk:
                session["jezyk"] = jezyk
            return redirect(url_for("master_widok"))
        blad = _("Błędna nazwa użytkownika lub hasło.")
    return render_template("login.html", blad=blad)


@app.route("/jezyk/<kod>")
def ustaw_jezyk(kod):
    # WHY: bez @login_required - login.html też ma być przetłumaczalny.
    if kod in i18n.JEZYKI:
        session["jezyk"] = kod
    return redirect(request.referrer or url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return redirect(url_for("master_widok"))


# =============================================================================
#  Master — usługa Ollama na TYM hoście + status wszystkich podłączonych
# =============================================================================
@app.route("/master")
@login_required
def master_widok():
    stan = wczytaj_stan()
    status = wczytaj_status()
    odswiezaj = czy_odswiezac("master", status)

    hosty_status = []
    for h in hosts_store.wczytaj_hosty():
        if h.get("master"):
            st = status
        else:
            st = hosts_store.wczytaj_status_hosta(h["nazwa"])
        hosty_status.append({"nazwa": h["nazwa"], "adres": h["adres"], "status": st})

    return render_template(
        "master.html",
        ollama=stan["ollama"],
        status=status,
        hosty_status=hosty_status,
        odswiezaj=odswiezaj,
    )


@app.route("/master/update", methods=["POST"])
@login_required
def master_update():
    stan = wczytaj_stan()
    akcja = request.form.get("akcja")

    if akcja == "zainstaluj_ollama":
        stan["ollama"]["zainstaluj_ollama"] = True
    elif akcja == "zapisz_env":
        env = stan["ollama"]["env"]

        def ustaw_tekst(nazwa, pole):
            wartosc = request.form.get(pole, "").strip()
            if wartosc:
                env[nazwa] = wartosc
            else:
                env.pop(nazwa, None)

        def ustaw_wybor(nazwa, pole):
            # WHY: pusta wartość z <select> to świadomy wybór "domyślne", tak
            # samo jak puste pole tekstowe — usuwa zmienną, nie ustawia "".
            wartosc = request.form.get(pole, "")
            if wartosc:
                env[nazwa] = wartosc
            else:
                env.pop(nazwa, None)

        ustaw_tekst("OLLAMA_KEEP_ALIVE", "keep_alive")
        ustaw_tekst("OLLAMA_CONTEXT_LENGTH", "context_length")
        ustaw_tekst("OLLAMA_MAX_LOADED_MODELS", "max_loaded_models")
        ustaw_tekst("OLLAMA_NUM_PARALLEL", "num_parallel")
        ustaw_wybor("OLLAMA_FLASH_ATTENTION", "flash_attention")
        ustaw_wybor("OLLAMA_KV_CACHE_TYPE", "kv_cache")

        # WHY: Vulkan zapisuje jawne "0" gdy wyłączony (nie usuwa zmiennej) -
        # 1:1 z zachowaniem Ollama Managera (ustaw_vulkan).
        env["OLLAMA_VULKAN"] = "1" if request.form.get("vulkan") else "0"

        # WHY: iGPU jest domyślnie WŁĄCZONE - zaznaczona kratka = wartość
        # domyślna (usuń zmienną), odznaczona = jawne "false".
        if request.form.get("igpu"):
            env.pop("OLLAMA_IGPU_ENABLE", None)
        else:
            env["OLLAMA_IGPU_ENABLE"] = "false"

        if request.form.get("host_lan"):
            env["OLLAMA_HOST"] = "0.0.0.0:11434"
        else:
            env.pop("OLLAMA_HOST", None)
    elif akcja == "start":
        stan["ollama"]["service_running"] = True
    elif akcja == "stop":
        stan["ollama"]["service_running"] = False
    elif akcja == "autostart_wlacz":
        stan["ollama"]["service_enabled"] = True
    elif akcja == "autostart_wylacz":
        stan["ollama"]["service_enabled"] = False

    zapisz_stan(stan)
    oznacz_oczekiwanie("master")
    return redirect(url_for("master_widok"))


# =============================================================================
#  Slave — zdalne hosty Ollamy: dodawanie/usuwanie, pobranie instalatora
# =============================================================================
@app.route("/slave")
@login_required
def slave_widok():
    hosty = []
    odswiezaj = False
    for h in hosts_store.wczytaj_slave_hosty():
        status = hosts_store.wczytaj_status_hosta(h["nazwa"])
        hosty.append({**h, "status": status})
        if czy_odswiezac(h["nazwa"], status):
            odswiezaj = True
    return render_template("slave.html", hosty=hosty, odswiezaj=odswiezaj)


@app.route("/slave/dodaj", methods=["POST"])
@login_required
def slave_dodaj():
    nazwa = request.form.get("nazwa", "").strip()
    ip = request.form.get("ip", "").strip()
    mac = request.form.get("mac", "").strip() or None

    if not NAZWA_HOSTA_WZORZEC.match(nazwa):
        flash(_("Nazwa hosta może zawierać tylko litery, cyfry i myślniki."))
        return redirect(url_for("slave_widok"))

    try:
        ipaddress.ip_address(ip)
    except ValueError:
        flash(_("Nieprawidłowy adres IP."))
        return redirect(url_for("slave_widok"))

    if mac and not wol.WZORZEC_MAC.match(mac):
        flash(_("Nieprawidłowy adres MAC (oczekiwany format: AA:BB:CC:DD:EE:FF)."))
        return redirect(url_for("slave_widok"))

    # WHY: jeśli user nie podał MAC ręcznie, spróbuj wykryć go sam z ARP -
    # działa tylko gdy host odpowiada TERAZ w sieci (patrz wol.znajdz_mac).
    if not mac:
        mac = wol.znajdz_mac(ip)
        if mac:
            flash(_("Wykryto adres MAC automatycznie: {mac}").format(mac=mac))

    try:
        hosts_store.dodaj_host(nazwa, ip, mac)
        zsynchronizuj_nfs_eksporty(hosts_store.wczytaj_slave_hosty())
    except ValueError as e:
        flash(str(e))

    return redirect(url_for("slave_widok"))


@app.route("/slave/<nazwa>/wykryj_mac", methods=["POST"])
@login_required
def slave_wykryj_mac(nazwa):
    host = hosts_store.znajdz_host(nazwa)
    if not host:
        flash(_("Nie znaleziono takiego hosta."))
        return redirect(url_for("slave_widok"))
    mac = wol.znajdz_mac(host["ip"])
    if mac:
        hosts_store.ustaw_mac(nazwa, mac)
        flash(_("Wykryto adres MAC automatycznie: {mac}").format(mac=mac))
    else:
        flash(_("Nie udało się wykryć adresu MAC — host musi teraz odpowiadać w sieci."))
    return redirect(url_for("slave_widok"))


@app.route("/slave/<nazwa>/wybudz", methods=["POST"])
@login_required
def slave_wybudz(nazwa):
    host = hosts_store.znajdz_host(nazwa)
    if not host or not host.get("mac"):
        flash(_("Ten host nie ma ustawionego adresu MAC."))
        return redirect(url_for("slave_widok"))
    wol.wyslij_magic_packet(host["mac"])
    return redirect(url_for("slave_widok"))


@app.route("/slave/<nazwa>/zasilanie", methods=["POST"])
@login_required
def slave_zasilanie(nazwa):
    # WHY: wyłączenie/restart/uśpienie to operacja uprzywilejowana na TAMTYM
    # hoście - panel zapisuje żądanie do JEGO state.json, nie woła niczego
    # bezpośrednio (patrz hosts_store.ustaw_zasilanie).
    host = hosts_store.znajdz_host(nazwa)
    akcja = request.form.get("akcja")
    if not host or akcja not in ("poweroff", "reboot", "suspend"):
        flash(_("Nie znaleziono takiego hosta."))
        return redirect(url_for("slave_widok"))
    hosts_store.ustaw_zasilanie(nazwa, akcja)
    oznacz_oczekiwanie(nazwa)
    return redirect(url_for("slave_widok"))


@app.route("/slave/<nazwa>/usun", methods=["POST"])
@login_required
def slave_usun(nazwa):
    try:
        hosts_store.usun_host(nazwa)
        zsynchronizuj_nfs_eksporty(hosts_store.wczytaj_slave_hosty())
    except ValueError as e:
        flash(str(e))
    return redirect(url_for("slave_widok"))


@app.route("/slave/<nazwa>/instalator")
@login_required
def slave_instalator(nazwa):
    host = hosts_store.znajdz_host(nazwa)
    if not host or host.get("master"):
        flash(_("Nie znaleziono takiego hosta."))
        return redirect(url_for("slave_widok"))

    tresc = install_generator.zbuduj_install_script(host["nazwa"], host["ip"])
    return Response(
        tresc,
        mimetype="text/x-shellscript",
        headers={"Content-Disposition": f"attachment; filename=install-{nazwa}.sh"},
    )


# =============================================================================
#  LLM — agregator LiteLLM: usługa + wybór modeli per host
# =============================================================================
@app.route("/llm")
@login_required
def llm_widok():
    hosty = hosts_store.wczytaj_hosty()
    wykryte = litellm.wykryj_wszystkie_modele(hosty)

    siatka = []
    for h in hosty:
        modele_hosta = sorted({model for nazwa, model, _ in wykryte if nazwa == h["nazwa"]})
        siatka.append(
            {
                "nazwa": h["nazwa"],
                "master": h.get("master", False),
                "modele": [
                    {"nazwa": m, "wlaczony": m in h.get("modele_llm", [])} for m in modele_hosta
                ],
            }
        )

    litellm_stan = {
        "zainstalowane": litellm.zainstalowane(),
        "dziala": litellm.dziala(),
        "autostart": litellm.autostart_wlaczony(),
    }

    # WHY: zbalansowane/modele_wszystkie liczone z tych samych włączonych
    # checkboxów co siatka wyżej - fallback/priorytet/context-window w UI mają
    # wybierać TYLKO spośród modeli faktycznie wystawionych, nie ze wszystkiego,
    # co jest zainstalowane na hostach (patrz CEL pkt 3-4).
    zbalansowane = litellm.modele_zbalansowane(hosty)
    modele_wszystkie = litellm.modele_wystawione(hosty)
    ustawienia = litellm_ustawienia.wczytaj_ustawienia()

    return render_template(
        "llm.html",
        litellm=litellm_stan,
        siatka=siatka,
        ustawienia=ustawienia,
        strategie=litellm_ustawienia.STRATEGIE_ROUTINGU,
        opisy_strategii=litellm_ustawienia.OPISY_STRATEGII,
        zbalansowane=zbalansowane,
        modele_wszystkie=modele_wszystkie,
    )


@app.route("/llm/usluga", methods=["POST"])
@login_required
def llm_usluga():
    # WHY: LiteLLM to usługa systemd --user, bez roota — w przeciwieństwie do
    # usługi Ollama, panel steruje nią bezpośrednio, bez state.json/demona
    # (patrz litellm_manager.py, docstring modułu).
    akcja = request.form.get("akcja")
    try:
        if akcja == "zainstaluj":
            litellm.zainstaluj()
        elif akcja == "start":
            litellm.uruchom()
        elif akcja == "stop":
            litellm.zatrzymaj()
        elif akcja == "autostart_wlacz":
            litellm.autostart(True)
        elif akcja == "autostart_wylacz":
            litellm.autostart(False)
    except RuntimeError as e:
        flash(str(e))

    return redirect(url_for("llm_widok"))


@app.route("/llm/zapisz_modele", methods=["POST"])
@login_required
def llm_zapisz_modele():
    for h in hosts_store.wczytaj_hosty():
        modele = request.form.getlist(f"modele__{h['nazwa']}")
        hosts_store.ustaw_modele_llm(h["nazwa"], modele)
    return redirect(url_for("llm_widok"))


@app.route("/llm/zapisz_routing", methods=["POST"])
@login_required
def llm_zapisz_routing():
    # WHY: ustawienia zapisujemy niezależnie od configu YAML - dopiero
    # start/restart usługi (llm_usluga -> litellm.uruchom/autostart) wywołuje
    # zapisz_config i realnie je stosuje, tak samo jak dziś działa wybór
    # modeli w llm_zapisz_modele powyżej.
    hosty = hosts_store.wczytaj_hosty()
    modele_wszystkie = litellm.modele_wystawione(hosty)
    zbalansowane = litellm.modele_zbalansowane(hosty)

    routing_strategy = request.form.get("routing_strategy", "")
    if routing_strategy not in litellm_ustawienia.STRATEGIE_ROUTINGU:
        flash(_("Nieprawidłowa strategia routingu."))
        return redirect(url_for("llm_widok"))

    def _dodatnia_liczba(pole, etykieta):
        wartosc = request.form.get(pole, "")
        if not wartosc.isdigit() or int(wartosc) <= 0:
            raise ValueError(etykieta)
        return int(wartosc)

    try:
        num_retries = _dodatnia_liczba("num_retries", _("Liczba ponowień"))
        timeout = _dodatnia_liczba("timeout", _("Limit czasu"))
        cooldown_time = _dodatnia_liczba("cooldown_time", _("Czas wychłodzenia"))
        allowed_fails = _dodatnia_liczba("allowed_fails", _("Dozwolone błędy"))
    except ValueError as e:
        flash(
            _("Nieprawidłowa wartość pola „{pole}” — wymagana liczba całkowita dodatnia.").format(
                pole=str(e)
            )
        )
        return redirect(url_for("llm_widok"))

    # WHY: fallback/context-window mogą wskazywać TYLKO na inny model z listy
    # faktycznie wystawionych (nigdy na siebie samego) - inaczej LiteLLM
    # dostałby odniesienie do modelu, którego nie ma w model_list.
    fallbacks = {}
    for model in modele_wszystkie:
        wybrany = request.form.get(f"fallback__{model}", "").strip()
        if wybrany and wybrany != model and wybrany in modele_wszystkie:
            fallbacks[model] = wybrany

    context_window_wlaczone = request.form.get("context_window_wlaczone") == "on"
    context_window_fallbacks = {}
    for model in modele_wszystkie:
        wybrany = request.form.get(f"context_window__{model}", "").strip()
        if wybrany and wybrany != model and wybrany in modele_wszystkie:
            context_window_fallbacks[model] = wybrany

    priorytet = {}
    for model, hosty_modelu in zbalansowane.items():
        wpisy_hosta = {}
        for nazwa_hosta in hosty_modelu:
            wartosc = request.form.get(f"priorytet__{model}__{nazwa_hosta}", "").strip()
            if not wartosc:
                continue
            if not wartosc.isdigit() or int(wartosc) <= 0:
                flash(
                    _(
                        "Priorytet musi być liczbą całkowitą dodatnią ({model} / {host})."
                    ).format(model=model, host=nazwa_hosta)
                )
                return redirect(url_for("llm_widok"))
            wpisy_hosta[nazwa_hosta] = int(wartosc)
        if wpisy_hosta:
            priorytet[model] = wpisy_hosta

    ustawienia = {
        **litellm_ustawienia.wczytaj_ustawienia(),
        "routing_strategy": routing_strategy,
        "num_retries": num_retries,
        "timeout": timeout,
        "cooldown_time": cooldown_time,
        "allowed_fails": allowed_fails,
        "fallbacks": fallbacks,
        "context_window_fallbacks_wlaczone": context_window_wlaczone,
        "context_window_fallbacks": context_window_fallbacks,
        "priorytet": priorytet,
    }
    litellm_ustawienia.zapisz_ustawienia(ustawienia)
    flash(_("Zapisano ustawienia routingu. Restart usługi LiteLLM zastosuje je w configu."))
    return redirect(url_for("llm_widok"))


@app.route("/llm/config_continue")
@login_required
def llm_config_continue():
    # WHY: appka celowo NIGDY nie zapisuje ~/.continue/config.yaml sama - ten
    # plik należy do użytkownika i może mieć inne wpisy; automatyczny zapis
    # mógłby je nadpisać (ten sam wybór co w Ollama Managerze, DialogConfigContinue).
    modele = litellm.modele_wystawione(hosts_store.wczytaj_hosty())
    tresc = litellm.zbuduj_config_continue(modele)
    return render_template("config_continue.html", tresc=tresc, modele=modele)


# =============================================================================
#  WebUI — Open WebUI podpięte pod LiteLLM (patrz openwebui_manager.py)
# =============================================================================
@app.route("/webui")
@login_required
def webui_widok():
    webui_stan = {
        "zainstalowane": webui.zainstalowane(),
        "dziala": webui.dziala(),
        "autostart": webui.autostart_wlaczony(),
    }
    return render_template("webui.html", webui=webui_stan, webui_url=webui.WEBUI_URL)


@app.route("/webui/usluga", methods=["POST"])
@login_required
def webui_usluga():
    # WHY: Open WebUI, tak samo jak LiteLLM, to usługa systemd --user bez roota
    # (patrz openwebui_manager.py, docstring modułu) - sterowana bezpośrednio.
    akcja = request.form.get("akcja")
    try:
        if akcja == "zainstaluj":
            webui.zainstaluj()
        elif akcja == "start":
            webui.uruchom()
        elif akcja == "stop":
            webui.zatrzymaj()
        elif akcja == "autostart_wlacz":
            webui.autostart(True)
        elif akcja == "autostart_wylacz":
            webui.autostart(False)
    except RuntimeError as e:
        flash(str(e))

    return redirect(url_for("webui_widok"))


# =============================================================================
#  Modele — lista/pobierz/usuń na DOWOLNYM hoście (master albo slave), zero
#  roota, wprost przez /api/... (patrz README.md, "Panel WWW + logowanie")
# =============================================================================
def _host_i_klient(nazwa):
    host = hosts_store.znajdz_host(nazwa)
    if not host:
        return None, None
    return host, OllamaClient(host["adres"])


@app.route("/modele/<nazwa>")
@login_required
def modele_widok(nazwa):
    host, klient = _host_i_klient(nazwa)
    if not host:
        flash(_("Nie znaleziono takiego hosta."))
        return redirect(url_for("master_widok"))

    modele = klient.list_models()
    zaladowane = {m["name"] for m in klient.list_loaded()}

    return render_template(
        "modele.html",
        host=host,
        modele=modele,
        zaladowane=zaladowane,
        pobrania=pobierania.stan_hosta(nazwa),
        odswiezaj=pobierania.aktywne(nazwa),
    )


@app.route("/modele/<nazwa>/usun", methods=["POST"])
@login_required
def modele_usun(nazwa):
    _host, klient = _host_i_klient(nazwa)
    model = request.form.get("model", "")
    if klient:
        try:
            klient.delete_model(model)
        except requests.RequestException as e:
            flash(_("Błąd usuwania {model}: {blad}").format(model=model, blad=e))
    return redirect(url_for("modele_widok", nazwa=nazwa))


@app.route("/modele/<nazwa>/pobierz", methods=["POST"])
@login_required
def modele_pobierz(nazwa):
    host, klient = _host_i_klient(nazwa)
    model = request.form.get("model", "").strip()
    if klient and model:
        pobierania.rozpocznij(nazwa, model, klient)
    return redirect(url_for("modele_widok", nazwa=nazwa))


if __name__ == "__main__":
    # WHY: tylko do lokalnego developmentu - prawdziwe uruchomienie idzie przez
    # `waitress-serve` (patrz systemd/ollama-manager-web.service), bo wbudowany
    # serwer Flask ostrzega o sobie samym "nie używaj tego produkcyjnie", a
    # debug=True na hoście widocznym w całym LAN (decyzja #1) to zdalne
    # wykonanie kodu przez debugger Werkzeuga, nie tylko wygodniejsze tracebacki.
    debug = os.environ.get("OLLAMA_MANAGER_DEBUG") == "1"
    app.run(host="0.0.0.0", port=5000, debug=debug)
