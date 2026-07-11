#!/usr/bin/env python3
"""Panel WWW ollama-manager — trzy zakładki: Master / Slave / LLM.

Ten proces NIGDY nie woła systemctl/pkexec dla usługi Ollama bezpośrednio —
tylko zapisuje docelowy stan do state.json, który stosuje lokalny
ollama-manager-daemon (patrz README.md, sekcja Architektura). LiteLLM (usługa
systemd --user, bez roota) i operacje na modelach są sterowane bezpośrednio.

- Master: usługa Ollama i jej zmienne środowiskowe NA TYM hoście + status
  wszystkich podłączonych hostów (master + slave'y).
- Slave: dodawanie/usuwanie zdalnych hostów Ollamy, pobranie instalatora dla
  każdego z nich.
- LLM: sterowanie agregatorem LiteLLM + wybór, które modele z których hostów
  ma wystawiać.
- Modele (/modele/<nazwa>, linkowane z Master/Slave): lista/pobierz/usuń modele
  na DOWOLNYM hoście, bezpośrednio przez /api/... (zero roota, jak LiteLLM).
"""

import ipaddress
import os
import re
import secrets
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, Response, flash, redirect, render_template, request, session, url_for

import hosts_store
import install_generator
import litellm_manager as litellm
import pobierania
from auth import zweryfikuj
from ollama_client import OllamaClient
from state_store import wczytaj_stan, wczytaj_status, zapisz_stan, zsynchronizuj_nfs_eksporty

app = Flask(__name__)

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
            session.clear()
            session["zalogowany"] = True
            session["username"] = username
            return redirect(url_for("master_widok"))
        blad = "Błędna nazwa użytkownika lub hasło."
    return render_template("login.html", blad=blad)


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

    hosty_status = []
    for h in hosts_store.wczytaj_hosty():
        if h.get("master"):
            st = status
        else:
            st = hosts_store.wczytaj_status_hosta(h["nazwa"])
        hosty_status.append({"nazwa": h["nazwa"], "adres": h["adres"], "status": st})

    return render_template(
        "master.html", ollama=stan["ollama"], status=status, hosty_status=hosty_status
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
    return redirect(url_for("master_widok"))


# =============================================================================
#  Slave — zdalne hosty Ollamy: dodawanie/usuwanie, pobranie instalatora
# =============================================================================
@app.route("/slave")
@login_required
def slave_widok():
    hosty = []
    for h in hosts_store.wczytaj_slave_hosty():
        hosty.append({**h, "status": hosts_store.wczytaj_status_hosta(h["nazwa"])})
    return render_template("slave.html", hosty=hosty)


@app.route("/slave/dodaj", methods=["POST"])
@login_required
def slave_dodaj():
    nazwa = request.form.get("nazwa", "").strip()
    ip = request.form.get("ip", "").strip()

    if not NAZWA_HOSTA_WZORZEC.match(nazwa):
        flash("Nazwa hosta może zawierać tylko litery, cyfry i myślniki.")
        return redirect(url_for("slave_widok"))

    try:
        ipaddress.ip_address(ip)
    except ValueError:
        flash("Nieprawidłowy adres IP.")
        return redirect(url_for("slave_widok"))

    try:
        hosts_store.dodaj_host(nazwa, ip)
        zsynchronizuj_nfs_eksporty(hosts_store.wczytaj_slave_hosty())
    except ValueError as e:
        flash(str(e))

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
        flash("Nie znaleziono takiego hosta.")
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
    return render_template("llm.html", litellm=litellm_stan, siatka=siatka)


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
        flash("Nie znaleziono takiego hosta.")
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
    _, klient = _host_i_klient(nazwa)
    model = request.form.get("model", "")
    if klient:
        try:
            klient.delete_model(model)
        except requests.RequestException as e:
            flash(f"Błąd usuwania {model}: {e}")
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
