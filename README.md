# Home AI Farm — zarys projektu

Panel WWW (z logowaniem) do zarządzania lokalnym stackiem Ollama/LiteLLM/Open WebUI
na wielu hostach w domowej sieci (workstation + serwer BC-250), **bez okienek** —
odpowiednik funkcji [Ollama Manager](../Ollama-manager) (aplikacji PyQt6/KDE), tylko
dostępny z przeglądarki.

Stan: **działający szkielet, wielohostowość domknięta architektonicznie** —
[`daemon/`](daemon/) (pętla stan.json → systemctl/override.conf/exportfs →
status.json, wszystkie zmienne z zakładki Zaawansowane, instalacja Ollamy,
zarządzanie eksportami NFS) i [`web/`](web/) (logowanie, trzy zakładki
Master/Slave/LLM, LiteLLM bezpośrednio przez `systemd --user`) działają
end-to-end. [`install.sh`](install.sh) stawia Ollamę + LiteLLM + serwer NFS +
oba komponenty na hoście zarządzającym za jednym razem; zakładka Slave generuje
`install-<hostname>.sh` dla zdalnych hostów. Niesprawdzone na żywym BC-250 —
logika przetestowana z zamockowanym `exportfs`/`systemctl`, nie na prawdziwym
sprzęcie.

## Skąd to się wzięło

Ollama Manager (PyQt6, `~/Projekty/Ollama-manager`) już umie zarządzać usługą Ollama,
modelami, agregatorem LiteLLM i Open WebUI na wielu hostach naraz — ale wymaga okna
(GUI) i lokalnego uruchomienia na maszynie, z której się nim steruje. Padło pytanie
"czy dałoby się to samo zrobić przez WWW" — i tak, z jednym zastrzeżeniem: sterowanie
usługą systemd (start/stop/autostart, zmienne środowiskowe) wymaga uprawnień roota
(`pkexec` w appce), a przeglądarka nie ma jak wywołać graficznego promptu polkit na
zdalnej maszynie. Stąd potrzebny jest lokalny agent (demon) na każdym hoście.

## Architektura

Dwa oddzielne procesy na każdym hoście, żeby proces z uprawnieniami roota **nigdy nie
dotykał sieci**:

```
┌─────────────────────────────┐         ┌──────────────────────────────┐
│  ollama-manager-web         │         │  ollama-manager-daemon        │
│  (user, BEZ roota)          │         │  (root, systemd system unit)  │
│                              │         │                                │
│  - panel WWW + logowanie    │         │  - inotify na plik stanu       │
│  - operacje na modelach     │  plik   │  - diff: co się zmieniło        │
│    -> bezpośrednio /api/... │ stanu   │  - override.conf + reload/     │
│    (bez roota, jak w Ollama │ ──────► │    restart/enable/disable       │
│    Managerze dziś)          │ (JSON)  │  - zapisuje status.json z       │
│  - zapisuje "co user chce"  │ ◄────── │    wynikiem (OK/błąd)           │
│    do pliku stanu           │ status  │                                │
└─────────────────────────────┘ .json   └──────────────────────────────┘
```

Kluczowa zasada: jedyny kontakt roota (daemon) ze światem to plik na dysku, zero
portu/API sieciowego. Cała powierzchnia ataku uprzywilejowanego procesu to "czy ten
plik ma sensowną zawartość" — dokładnie tak bezpieczne jak dzisiejszy `pkexec` +
treść na STDIN w Ollama Managerze, tylko root jest teraz stały w tle zamiast pojawiać
się na chwilę przy każdym kliknięciu.

### Plik stanu (`state.json`, zapisuje web, czyta daemon)

```json
{
  "ollama": {
    "service_running": true,
    "service_enabled": true,
    "env": {
      "OLLAMA_KEEP_ALIVE": "30m",
      "OLLAMA_CONTEXT_LENGTH": "32768",
      "OLLAMA_HOST": ""
    }
  }
}
```

### Pętla demona

1. `inotify` na katalog (nie na sam plik — atomowy zapis to zwykle rename, trzeba
   łapać `IN_MOVED_TO`/`IN_CLOSE_WRITE`, nie `IN_MODIFY`).
2. Wczytaj `state.json`, porównaj z aktualnym stanem systemu (odpowiedniki
   `_usluga_env_wszystkie()` i `systemctl is-active`/`is-enabled` z Ollama Managera —
   ten kod już istnieje i działa, do przeniesienia/reużycia).
3. Zastosuj TYLKO różnice: przepisz `override.conf`, `daemon-reload`,
   `restart`/`start`/`stop`/`enable`/`disable` — żeby zbędny zapis pliku nie
   restartował usługi bez potrzeby (i nie wyładowywał modeli z VRAM bez powodu).
4. Zapisz `status.json` (co zastosowano, kiedy, czy był błąd) — panel WWW to czyta
   i pokazuje np. "zastosowano o 14:32" albo komunikat błędu.

### Panel WWW + logowanie

- Prosty formularz login/hasło — hash w lokalnym pliku, NIE baza danych (narzędzie
  osobiste, jeden użytkownik, zero potrzeby na wielu userów/OAuth).
- Sesja przez podpisane ciasteczko (np. wbudowany mechanizm sesji Flask/FastAPI).
- Operacje na modelach (lista/pobierz/usuń, w pamięci) — **bez zmian względem
  dzisiejszej appki**: idą wprost do `/api/tags`, `/api/pull`, `/api/delete` na
  wybranym hoście (`/modele/<nazwa>`, linkowane z Master/Slave), zero roota
  potrzebne, `OllamaClient` (`web/ollama_client.py`) przeniesiony z Ollama
  Managera 1:1. Pobieranie modelu strumieniuje minuty do godzin — leci w wątku
  w tle (`web/pobierania.py`, stan w pamięci procesu), strona odświeża się co
  2s przez zwykłe `<meta http-equiv="refresh">` dopóki coś trwa, bez JS.
- Operacje na usłudze/env (start/stop/autostart, Zaawansowane) — zapisują
  `state.json`, NIE wołają niczego uprzywilejowanego bezpośrednio.

**Trzy zakładki** (`web/templates/`):
- **Master** — usługa Ollama i jej zmienne środowiskowe NA TYM hoście + tabela
  statusu wszystkich podłączonych hostów (master + slave'y, czytana z ich
  `status.json`).
- **Slave** — dodawanie/usuwanie zdalnych hostów (`web/hosts_store.py`,
  `hosts.json`) + pobranie wygenerowanego `install-<nazwa>.sh`
  (`web/install_generator.py` — wkleja kod demona wprost do skryptu, zero
  osobnego hostingu plików).
- **LLM** — sterowanie usługą LiteLLM + siatka host×model: user świadomie
  zaznacza, które modele z których hostów agregator ma wystawiać (nie
  wszystko, co akurat jest zainstalowane).

Pierwszy wpis w `hosts.json` to zawsze `master` (ten host, auto-tworzony,
niekasowalny z zakładki Slave) — dzięki temu jego modele są tak samo
wybieralne w LLM jak modele zdalnych hostów, bez specjalnego przypadku w kodzie.

### Wielohostowość — NFS + instalacja nowego hosta

Sterowanie usługą jest z natury lokalne dla hosta, na którym stoi (nie da się zdalnie
`systemctl restart` na innej maszynie) — więc **każdy host z Ollamą (workstation,
BC-250, ...) potrzebuje własnej instancji demona**. Workstation, na którym stoi panel
WWW, jest jednocześnie serwerem NFS **i** jednym z zarządzanych hostów — ma więc już
swój lokalny `ollama-manager-daemon` z rootem. Zamiast dawać panelowi WWW nowe
uprawnienia do edycji `/etc/exports`, obowiązuje ta sama zasada co dla systemd (patrz
Architektura wyżej): **panel WWW zapisuje "chcę taki eksport dla hosta X" do pliku
stanu, a lokalny demon na workstation to stosuje** — root nadal nigdy nie dotyka
sieci bezpośrednio z poziomu panelu WWW.

- Katalog bazowy na serwerze: `/srv/ollama-manager/hosts/`.
- Dla każdego dodanego hosta osobny podkatalog, np. `/srv/ollama-manager/hosts/bc250/`,
  z **osobnym wpisem ograniczonym do IP tylko tego jednego hosta**:
  ```
  /srv/ollama-manager/hosts/bc250  192.168.X.Y(rw,sync,no_subtree_check,root_squash)
  ```
  Pełna izolacja — host fizycznie nie może zamontować cudzego katalogu, nawet gdyby
  znał ścieżkę. Wpisy lądują w `/etc/exports.d/ollama-manager.exports` (natywny
  mechanizm dołączania z `exports(5)`), NIE w głównym `/etc/exports` — zero ryzyka
  nadpisania eksportów admina niezwiązanych z tym projektem.
- Lokalny demon na workstation, po dopisaniu nowego hosta do stanu (`state.json`,
  klucz `nfs_eksporty` — zapisywany przez zakładkę Slave przy dodaniu/usunięciu
  hosta), sam tworzy podkatalog, przepisuje `ollama-manager.exports` i robi
  `exportfs -ra` — TYLKO gdy treść faktycznie się zmieniła, analogicznie jak dziś
  robi `daemon-reload` po zmianie `override.conf` (`daemon/ollama_manager_daemon.py`,
  `zastosuj_eksporty_nfs`).

#### Dodawanie nowego hosta

1. Admin w panelu WWW podaje nazwę hosta i jego adres IP (do restrykcji eksportu NFS).
2. Panel WWW zapisuje nowy wpis do stanu → lokalny demon na workstation tworzy
   podkatalog, dopisuje eksport, `exportfs -ra`.
3. Panel WWW generuje gotowy `install-<hostname>.sh` (z wypełnionym IP serwera,
   ścieżką eksportu, nazwą hosta) do pobrania/skopiowania — **admin sam uruchamia go
   przez SSH na nowym hoście**; panel nie przechowuje żadnych poświadczeń SSH do
   obcych maszyn.
4. Skrypt na nowym hoście:
   - instaluje **Ollamę** (jeśli jeszcze jej nie ma) oraz pakiety potrzebne do
     działania demona i montażu (`nfs-common`, Python + zależności demona jak
     `watchdog`/`inotify_simple`),
   - montuje eksport NFS (wpis w `/etc/fstab`:
     `workstation_ip:/srv/ollama-manager/hosts/<hostname> /var/lib/ollama-manager/state nfs defaults 0 0`),
   - instaluje i włącza `ollama-manager-daemon.service`.

## Decyzje (rozstrzygnięte 2026-07-11)

1. **Gdzie fizycznie żyje panel WWW** — widoczny w całym LAN, w tym z telefonu, nie
   tylko `localhost`. Konsekwencja: logowanie/TLS to konieczność (nie formalność), i
   transport pliku stanu do zdalnych hostów (BC-250) jest wymagany od startu, nie
   opcją na później.
2. **Jak plik stanu dociera do demona na ZDALNYM hoście** (BC-250) — wspólny montaż
   sieciowy (NFS/sshfs): katalog stanu zamontowany identycznie na obu maszynach,
   demon widzi zmiany przez zwykły `inotify`, zero nowego portu/API na demonie —
   zachowuje zasadę "root nigdy nie dotyka sieci" z sekcji Architektura.
3. **Osobny katalog/projekt czy część repo Ollama Manager** — zostaje jako osobne
   repo `Home AI FARM`, logika sterowania usługą/modelami przenoszona z Ollama
   Managera przez kopiowanie/adaptację kodu, nie współdzielenie repo.
4. **Stack technologiczny** — Flask + zwykły HTML/Jinja, bez frontendowego
   frameworka — zgodnie z zasadą "prostota ponad wszystko" z Ollama Managera.

## Stan implementacji

- [x] Demon ([`daemon/ollama_manager_daemon.py`](daemon/ollama_manager_daemon.py)) —
      pętla `inotify` → diff → `systemctl`/override.conf/`exportfs` → `status.json`,
      wszystkie osiem zmiennych z zakładki Zaawansowane, instalacja Ollamy na
      żądanie (`zainstaluj_ollama`), zarządzanie eksportami NFS per-host
      (`nfs_eksporty` w state.json → `zastosuj_eksporty_nfs`).
- [x] Panel WWW ([`web/`](web/)) — logowanie (hash w pliku, `manage_users.py`),
      trzy zakładki Master/Slave/LLM (patrz "Panel WWW + logowanie" wyżej),
      LiteLLM sterowany bezpośrednio (`litellm_manager.py`, `systemd --user`,
      bez roota), własny CSS z trybem ciemnym (`web/static/style.css`).
- [x] `install.sh` — instaluje Ollamę + LiteLLM + `nfs-kernel-server` + oba
      komponenty na hoście zarządzającym za jednym razem, pomija to, co już
      jest zainstalowane.
- [x] Lista hostów w UI (`web/hosts_store.py` + zakładka Slave) — dodawanie/
      usuwanie z walidacją nazwy/IP, `master` auto-tworzony i niekasowalny,
      dodanie/usunięcie od razu synchronizuje `nfs_eksporty` w state.json.
- [x] Generowanie `install-<hostname>.sh` dla ZDALNYCH hostów z panelu WWW
      (`web/install_generator.py`, przycisk w zakładce Slave) — wkleja kod
      demona wprost do skryptu.
- [x] Serwer NFS na workstation — eksport per-host w `/etc/exports.d/ollama-manager.exports`
      (nie w głównym `/etc/exports`), ograniczony do IP hosta, zarządzany przez
      demona. Przetestowane z zamockowanym `exportfs`/`systemctl` (brak systemd
      w środowisku deweloperskim), NIE na żywym BC-250 — pierwsze prawdziwe
      dodanie zdalnego hosta wciąż warto przejść krok po kroku.
- [x] Zarządzanie modelami (`web/ollama_client.py`, `web/pobierania.py`,
      `/modele/<nazwa>`) — lista z rozmiarem i info "w pamięci" (`/api/ps`),
      usuwanie, pobieranie nowego modelu w tle z paskiem postępu (bez JS,
      `<meta refresh>`). Działa na master i na każdym slave.
- [x] Config Continue.dev (`litellm_manager.zbuduj_config_continue`,
      `/llm/config_continue`) — zbudowany z modeli faktycznie wystawionych przez
      LiteLLM (te same, zaznaczone w zakładce LLM), pokazany do ręcznego
      wklejenia/scalenia z `~/.continue/config.yaml` — panel NIGDY nie zapisuje
      tego pliku sam (ten sam wybór co w Ollama Managerze: plik należy do usera).

## Brakuje / świadomie pominięte

- **Zarządzanie zasilaniem hostów** — przydatne przy większych klastrach: część
  maszyn będzie czasem wyłączona, trzeba je wybudzić, a czasem trzeba je
  zdalnie wyłączyć.
  - **Wybudzanie (Wake-on-LAN)** — magic packet wysyłany z hosta zarządzającego
    (zwykły UDP broadcast, biblioteka standardowa `socket`, zero roota) do
    każdego hosta w tej samej sieci L2. Wymaga dopisania pola `mac` do wpisu
    hosta (`web/hosts_store.py`, `hosts.json`) i przycisku "Wybudź" w zakładce
    Slave.
  - **Wyłączanie/uśpienie** — to operacja uprzywilejowana na ZDALNYM hoście, więc
    ten sam mechanizm co reszta (`state.json` → demon na tamtym hoście stosuje
    `systemctl poweroff`/`suspend`), analogicznie do `zainstaluj_ollama` — nowa
    flaga per host, nie bezpośrednie wywołanie z panelu WWW.
- **Open WebUI** — README go wymienia jako część stosu (nagłówek pliku), ale
  panel WWW go jeszcze nie dotyka. W Ollama Managerze to ten sam wzorzec co
  LiteLLM (`uv tool install`, `systemd --user`) — do przeniesienia analogicznie
  do `litellm_manager.py`, jeśli okaże się potrzebne.
- **TLS** — świadomie POMINIĘTE w tym repo. Panel WWW zostaje na czystym HTTP;
  TLS (szyfrowanie połączenia) serwuje reverse proxy przed panelem, poza tym
  projektem (Caddy/nginx/inny) — nie ma potrzeby dublować tego w Flasku.
- **Serwer produkcyjny WSGI** zamiast wbudowanego serwera deweloperskiego Flask
  (`app.run()`) — wystarczające dla ruchu jednego użytkownika w LAN, ale Flask
  sam o tym ostrzega przy starcie.
- **Walidacja na żywym sprzęcie** — cała ścieżka NFS/eksporty/zdalny demon
  przetestowana wyłącznie z zamockowanym `systemctl`/`exportfs` w środowisku
  bez systemd. Pierwsze prawdziwe dodanie BC-250 (albo innego hosta) wciąż
  wymaga przejścia krok po kroku i weryfikacji na miejscu.

## Powiązane projekty

- [`~/Projekty/Ollama-manager`](../Ollama-manager) — appka PyQt6/KDE, źródło całej
  logiki sterowania usługą/modelami do przeniesienia tutaj. Patrz jej `CLAUDE.md` po
  szczegóły istniejących mechanizmów (`usluga_ustaw_zmienna`, `_zbuduj_config_litellm`,
  `_wykryj_modele_na_serwerach` itd.) - większość tego kodu da się reużyć praktycznie
  bez zmian, bo już jest odseparowana od GUI (patrz sekcja "Rozdział warstw" tamtego
  pliku).
