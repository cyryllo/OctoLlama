#!/usr/bin/env python3
"""ollama-manager-daemon — lokalny, root-owy demon sterujący usługą Ollama na tym hoście.

Czyta docelowy stan z state.json (zapisywanego przez panel WWW, dostarczanego tu
przez montaż NFS — patrz README.md, sekcja "Wielohostowość"), porównuje z aktualnym
stanem systemu i stosuje TYLKO różnice. Wynik zapisuje do status.json. Jedyny kontakt
tego procesu ze światem to plik na dysku — zero portu/API sieciowego.
"""

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

SERVICE_NAME = "ollama"
# WHY: ta sama zmienna środowiskowa co w web/state_store.py — oba procesy muszą
# patrzeć na ten sam katalog (lokalny na workstation, zamontowany NFS na hostach
# zdalnych jak BC-250).
STATE_DIR = Path(os.environ.get("OLLAMA_MANAGER_STATE_DIR", "/var/lib/ollama-manager/state"))
STATE_PATH = STATE_DIR / "state.json"
STATUS_PATH = STATE_DIR / "status.json"
OVERRIDE_PATH = Path("/etc/systemd/system") / f"{SERVICE_NAME}.service.d" / "override.conf"

# WHY: ta sama zmienna co OLLAMA_MANAGER_HOSTS_STATE_BASE w web/hosts_store.py -
# to katalog, w którym workstation (serwer NFS) trzyma stan każdego zdalnego hosta.
HOSTS_BASE = Path(os.environ.get("OLLAMA_MANAGER_HOSTS_STATE_BASE", "/srv/ollama-manager/hosts"))
# WHY: /etc/exports.d/*.exports to natywny, udokumentowany mechanizm nfs-utils
# (exports(5)) na dołączanie eksportów BEZ dotykania głównego /etc/exports -
# zero ryzyka nadpisania eksportów administratora niezwiązanych z tym projektem.
EXPORTS_PLIK = Path("/etc/exports.d/ollama-manager.exports")

log = logging.getLogger("ollama-manager-daemon")


# =============================================================================
#  Odczyt aktualnego stanu systemu (bez roota poza samym uruchomieniem demona)
# =============================================================================
def systemctl_query(arg):
    r = subprocess.run(
        ["systemctl", arg, SERVICE_NAME], capture_output=True, text=True, timeout=5
    )
    return r.stdout.strip()


def usluga_env_wszystkie():
    # WHY: parsujemy tylko format, który sami zapisujemy w zapisz_override() —
    # nie trzeba obsługiwać dowolnego syntaksu systemd.
    try:
        tresc = OVERRIDE_PATH.read_text()
    except OSError:
        return {}
    zmienne = {}
    for linia in tresc.splitlines():
        linia = linia.strip().removeprefix("Environment=").strip('"')
        if "=" in linia:
            klucz, wartosc = linia.split("=", 1)
            zmienne[klucz] = wartosc
    return zmienne


def ollama_zainstalowana():
    return shutil.which("ollama") is not None


def ollama_zainstaluj():
    # WHY: oficjalny skrypt instalacyjny z ollama.com - demon już jest rootem,
    # więc w przeciwieństwie do Ollama Managera (pkexec) leci wprost, bez
    # żadnego graficznego promptu.
    r = subprocess.run(
        ["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
        capture_output=True, text=True, timeout=None,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "instalacja Ollamy: nieznany błąd")


def stan_aktualny():
    return {
        "zainstalowana": ollama_zainstalowana(),
        "service_running": systemctl_query("is-active") == "active",
        "service_enabled": systemctl_query("is-enabled") == "enabled",
        "env": usluga_env_wszystkie(),
    }


# =============================================================================
#  Zastosowanie różnic (demon działa jako root — bez pkexec, w przeciwieństwie
#  do Ollama Managera, gdzie root pojawiał się tylko na chwilę przy kliknięciu)
# =============================================================================
def systemctl(*args):
    r = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"systemctl {' '.join(args)}: {r.stderr.strip() or r.returncode}")


def zapisz_override(env):
    tresc = "[Service]\n" + "".join(f'Environment="{k}={v}"\n' for k, v in env.items())
    OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_PATH.write_text(tresc)


def zastosuj(docelowy, obecny, zmiany):
    if docelowy.get("zainstaluj_ollama") and not obecny.get("zainstalowana"):
        ollama_zainstaluj()
        zmiany.append("Ollama: zainstalowano")

    env_docelowy = docelowy.get("env", {})
    env_obecny = obecny.get("env", {})
    env_zmieniony = env_docelowy != env_obecny

    if env_zmieniony:
        zapisz_override(env_docelowy)
        systemctl("daemon-reload")
        zmiany.append(f"env: {env_obecny} -> {env_docelowy}")

    docelowy_wlaczony = docelowy.get("service_enabled", False)
    if docelowy_wlaczony != obecny.get("service_enabled", False):
        systemctl("enable" if docelowy_wlaczony else "disable", SERVICE_NAME)
        zmiany.append(f"enabled: {docelowy_wlaczony}")

    docelowy_dziala = docelowy.get("service_running", False)
    obecnie_dziala = obecny.get("service_running", False)
    if env_zmieniony and docelowy_dziala and obecnie_dziala:
        # WHY: zmiana env przy już działającej usłudze wymaga restartu, żeby
        # w ogóle zaczęła obowiązywać — samo `daemon-reload` tego nie robi.
        systemctl("restart", SERVICE_NAME)
        zmiany.append("restart (zmiana env przy działającej usłudze)")
    elif docelowy_dziala != obecnie_dziala:
        systemctl("start" if docelowy_dziala else "stop", SERVICE_NAME)
        zmiany.append(f"running: {docelowy_dziala}")


# =============================================================================
#  Eksporty NFS per-host (zakładka Slave w panelu WWW) — jeden plik w
#  /etc/exports.d, ograniczenie do IP każdego hosta z osobna (izolacja: host
#  fizycznie nie może zamontować cudzego katalogu, patrz README "Wielohostowość").
# =============================================================================
def _tresc_exports(hosty_nfs):
    linie = ["# Zarządzane przez ollama-manager-daemon - NIE EDYTUJ RĘCZNIE.\n"]
    for h in hosty_nfs:
        sciezka = HOSTS_BASE / h["nazwa"]
        linie.append(f'{sciezka} {h["ip"]}(rw,sync,no_subtree_check,root_squash)\n')
    return "".join(linie)


def zastosuj_eksporty_nfs(hosty_nfs, zmiany):
    for h in hosty_nfs:
        (HOSTS_BASE / h["nazwa"]).mkdir(parents=True, exist_ok=True)

    tresc_nowa = _tresc_exports(hosty_nfs)
    tresc_stara = EXPORTS_PLIK.read_text() if EXPORTS_PLIK.exists() else ""
    if tresc_nowa == tresc_stara:
        return

    EXPORTS_PLIK.parent.mkdir(parents=True, exist_ok=True)
    EXPORTS_PLIK.write_text(tresc_nowa)
    try:
        r = subprocess.run(["exportfs", "-ra"], capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        raise RuntimeError("exportfs nie znaleziony - zainstaluj pakiet nfs-kernel-server")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "exportfs -ra: nieznany błąd")
    zmiany.append(f"NFS: eksporty zaktualizowane ({', '.join(h['nazwa'] for h in hosty_nfs) or 'brak hostów'})")


# =============================================================================
#  Zasilanie (zakładka Slave w panelu WWW) — jednorazowe polecenie, NIE stan
#  do utrzymywania jak reszta. Demon MUSI skasować flagę w state.json PRZED
#  wykonaniem (jedyne miejsce, gdzie demon świadomie pisze do state.json, nie
#  tylko status.json) - inaczej po wybudzeniu (Wake-on-LAN) zobaczyłby tę samą,
#  nieskasowaną flagę i natychmiast wyłączyłby maszynę ponownie, w pętli.
# =============================================================================
def zastosuj_zasilanie(cale_dane):
    akcja = (cale_dane.get("zasilanie") or {}).get("akcja")
    if not akcja:
        return False

    cale_dane["zasilanie"] = {"akcja": None}
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cale_dane, indent=2, ensure_ascii=False))
    tmp.rename(STATE_PATH)
    log.info("zasilanie: %s", akcja)

    polecenie = {"poweroff": "poweroff", "reboot": "reboot", "suspend": "suspend"}.get(akcja)
    if polecenie:
        subprocess.run(["systemctl", polecenie], timeout=10)
    return True


# =============================================================================
#  status.json — panel WWW to czyta i pokazuje np. "zastosowano o 14:32"
# =============================================================================
def zapisz_status(ok, zmiany, obecny, blad=None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tresc = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "zmiany": zmiany,
        "blad": blad,
        # WHY: zmierzony (nie tylko "życzeniowy") stan po zastosowaniu zmian -
        # panel WWW pokazuje TO, nie samo state.json, żeby np. "zainstalowana"
        # albo "działa" odzwierciedlało rzeczywistość, nie samo żądanie usera.
        "ollama": obecny,
    }
    # WHY: zapis przez tymczasowy plik + rename = atomowo, żeby panel WWW nigdy
    # nie odczytał częściowo zapisanego status.json.
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(tresc, indent=2, ensure_ascii=False))
    tmp.rename(STATUS_PATH)


def przetworz():
    try:
        cale_dane = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("nie można odczytać %s: %s", STATE_PATH, e)
        return

    try:
        if zastosuj_zasilanie(cale_dane):
            return  # WHY: maszyna się wyłącza/restartuje/usypia - reszta stanu nieistotna
    except Exception as e:
        log.exception("błąd zasilania")
        zapisz_status(False, [], stan_aktualny(), blad=str(e))
        return

    docelowy = cale_dane.get("ollama", {})
    hosty_nfs = cale_dane.get("nfs_eksporty", [])

    obecny = stan_aktualny()
    zmiany = []
    try:
        zastosuj(docelowy, obecny, zmiany)
        zastosuj_eksporty_nfs(hosty_nfs, zmiany)
    except Exception as e:
        log.exception("błąd stosowania stanu")
        zapisz_status(False, zmiany, stan_aktualny(), blad=str(e))
        return

    if zmiany:
        log.info("zastosowano: %s", "; ".join(zmiany))
    zapisz_status(True, zmiany, stan_aktualny())


# =============================================================================
#  Pętla demona — inotify na KATALOG, nie na sam plik (atomowy zapis to
#  zwykle rename, trzeba łapać IN_MOVED_TO/IN_CLOSE_WRITE, nie IN_MODIFY)
# =============================================================================
class StateHandler(FileSystemEventHandler):
    def on_moved(self, event):
        if Path(event.dest_path) == STATE_PATH:
            przetworz()

    def on_closed(self, event):
        if Path(event.src_path) == STATE_PATH:
            przetworz()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    przetworz()  # zastosuj stan już obecny w state.json przy starcie demona

    observer = Observer()
    observer.schedule(StateHandler(), str(STATE_DIR), recursive=False)
    observer.start()
    log.info("demon uruchomiony, obserwuję %s", STATE_DIR)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
