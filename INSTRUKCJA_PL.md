# OctoLlama — instrukcja dla użytkownika

Ten dokument tłumaczy, **jak OctoLlama jest zbudowana** (jeden master, wiele hostów z AI)
i **jak z niej korzystać na co dzień**. Zakłada, że instalacja z `install.sh` już się odbyła
(patrz README).

---

## 1. Jak to działa: jeden master, wiele hostów

W Twojej sieci domowej może pracować kilka komputerów z Ollamą — np. główny komputer,
mini-PC pod telewizorem i stary laptop w szafie. OctoLlama spina je w jedną całość:

- **Master** — komputer, na którym działa panel WWW OctoLlamy (port 5000).
  To jedyne miejsce, w które wchodzisz przeglądarką. Master ma też własną Ollamę,
  agregator LiteLLM (port 4000) i Open WebUI (czat).
- **Slave** — każdy dodatkowy komputer z Ollamą. Nie ma własnego panelu.
  Działa na nim tylko Ollama i mały demon, który wykonuje polecenia z mastera.

**Kluczowa zasada:** wszystkim sterujesz z jednego panelu na masterze.
Nie musisz logować się na slave'y przez SSH, żeby uruchomić usługę, pobrać model
czy zmienić ustawienia — robisz to z przeglądarki, także z telefonu.

### Co widzi użytkownik końcowy (czat, VS Code)

Modele ze **wszystkich** hostów są dostępne pod **jednym adresem**:
LiteLLM na masterze (port 4000). Dzięki temu:

- **Open WebUI** (czat) widzi jedną listę modeli — nie interesuje go,
  który komputer fizycznie liczy odpowiedź.
- **Continue.dev w VS Code** dostaje jedną konfigurację wygenerowaną w panelu
  i też korzysta ze wszystkich hostów naraz.
- W panelu (zakładka **LLM**) sam decydujesz, **które modele z których hostów**
  są w ogóle wystawione na zewnątrz.

### Jak master rozmawia ze slave'ami (dla ciekawych)

Panel WWW nigdy nie wykonuje poleceń jako root. Zamiast tego zapisuje
"czego chce użytkownik" do pliku `state.json`. Na każdym hoście (masterze i slave'ach)
działa lokalny demon z uprawnieniami roota, który obserwuje ten plik, wprowadza zmiany
(start/stop usługi, zmienne środowiskowe) i odpisuje wynik do `status.json`.

Do slave'ów pliki stanu trafiają przez **NFS**: master eksportuje osobny katalog
dla każdego hosta (ograniczony do jego adresu IP), a slave go montuje.
Demon roota nie otwiera żadnego portu sieciowego — jego jedynym kontaktem
ze światem jest plik na dysku. To celowa decyzja bezpieczeństwa.

---

## 2. Cztery zakładki panelu

Panel jest dostępny pod adresem `http://<adres-mastera>:<port>` (po zalogowaniu) —
port ustawiasz raz przy instalacji (domyślnie 5000, Enter przyjmuje domyślny).

### Master
Sterowanie Ollamą na hoście zarządzającym:

- start / stop / autostart usługi,
- zmienne środowiskowe wpływające na wydajność (rozmiar kontekstu, VRAM,
  Vulkan/iGPU, cache KV, dostępność w sieci),
- podgląd statusu wszystkich podłączonych hostów,
- przejście do zarządzania modelami (lista, rozmiary, co jest załadowane
  w pamięci, pobieranie z paskiem postępu, usuwanie).

### Slave
Dodawanie i zdejmowanie zdalnych hostów:

1. Kliknij **Dodaj hosta**, podaj nazwę i adres IP.
2. Panel wygeneruje gotowy skrypt `install-<nazwa>.sh`.
3. Pobierz go i uruchom **jeden raz, ręcznie** na nowej maszynie (np. przez SSH).
   Skrypt zainstaluje Ollamę, zamontuje katalog stanu przez NFS i postawi demona.
4. Od tej chwili host pojawia się w panelu i sterujesz nim jak masterem.

Tu też jest **zarządzanie zasilaniem**: Wake-on-LAN (obudzenie uśpionego
lub wyłączonego hosta — adres MAC wykrywany automatycznie z ARP albo wpisywany
ręcznie) oraz zdalne wyłączenie / restart / uśpienie. Typowy scenariusz:
mini-PC śpi i nie zużywa prądu, budzisz go z telefonu, gdy potrzebujesz
dodatkowej mocy, a po pracy usypiasz z tej samej zakładki.

### LLM
Sterowanie agregatorem LiteLLM:

- start / stop agregatora,
- wybór, **które modele z których hostów** są wystawione pod wspólnym adresem
  (port 4000, API zgodne z OpenAI),
- generowanie konfiguracji **Continue.dev** dla VS Code z aktualnie wystawionych
  modeli — do ręcznego wklejenia (panel nigdy nie nadpisuje Twojego pliku),
- **balansowanie obciążenia i niezawodność** (patrz niżej) — działa od razu
  po zaznaczeniu tego samego modelu na kilku hostach, reszta ustawień jest opcjonalna.

#### Balansowanie obciążenia i niezawodność

Jeśli ten sam model (dokładnie ta sama nazwa i tag, np. `qwen2.5-coder:14b`)
jest wystawiony na **więcej niż jednym hoście**, panel od razu pokazuje go
jako „zbalansowany na N hostach" — LiteLLM sam rozkłada między nie zapytania,
nic więcej nie trzeba klikać. Poniżej tego, w tej samej zakładce, są opcjonalne
ustawienia dostrajające to zachowanie:

- **Strategia routingu** — jak LiteLLM wybiera hosta spośród zbalansowanych:
  - `simple-shuffle` — losowo (domyślne, najprostsze),
  - `least-busy` — host, który akurat ma najmniej zapytań w toku,
  - `latency-based-routing` — host, który ostatnio odpowiadał najszybciej,
  - `usage-based-routing-v2` — uwzględnia limity zużycia (tokeny/zapytania na minutę).
- **Priorytet hostów** — dla modelu na kilku hostach możesz podać kolejność
  (niższa liczba = wyższy priorytet, np. szybszy komputer jako "1"). Zostaw
  puste, żeby zostało czyste losowe/automatyczne balansowanie.
- **Fallbacki** — dla każdego wystawionego modelu możesz wskazać model
  zapasowy z listy. Gdy zapytanie do modelu głównego się nie powiedzie,
  LiteLLM automatycznie spróbuje zapasowego (np. mały model 7b jako
  zapasowy dla większego 14b, gdyby ten akurat zawiódł).
- **Retry / timeout / cooldown** — ile razy ponowić nieudane zapytanie, po
  ilu sekundach uznać je za martwe, i na jak długo "wychłodzić" (czasowo
  pominąć) hosta po serii błędów.
- **Fallback przy przekroczeniu kontekstu** (osobny checkbox) — gdy
  zapytanie jest za długie na okno kontekstu małego modelu, włączenie tej
  opcji każe LiteLLM automatycznie spróbować wskazanego większego modelu
  zamiast po prostu zwrócić błąd.

**Ważne:** zapis tych ustawień (i wyboru wystawionych modeli wyżej) **od razu
restartuje działającą usługę LiteLLM**, więc zmiany obowiązują natychmiast —
nie trzeba osobno klikać Start/Stop wyżej.
Jeśli w `~/.config/octollama/litellm_config.yaml` masz coś dopisanego
ręcznie (np. model innego dostawcy z własnym kluczem API), panel tego nie
nadpisze — scala swoje wpisy z Twoimi przy każdym starcie usługi.

### WebUI
Start / stop **Open WebUI** — czatu w przeglądarce. Jest podpięty pod LiteLLM,
więc widzi dokładnie te modele, które wybrałeś w zakładce LLM — ze wszystkich
hostów naraz.

---

## 3. Typowe scenariusze

**Chcę porozmawiać z modelem z telefonu.**
Wejdź na panel → zakładka WebUI → uruchom Open WebUI → otwórz jego adres
w przeglądarce telefonu. Gotowe — czat działa na modelach z całej sieci.

**Chcę dołożyć drugi komputer z Ollamą.**
Zakładka Slave → Dodaj hosta → uruchom wygenerowany instalator na nowej maszynie.

**Model X ma być dostępny w VS Code.**
Zakładka Master → zarządzanie modelami → pobierz model (na wybranym hoście).
Potem zakładka LLM → zaznacz model do wystawienia → wygeneruj konfigurację
Continue.dev → wklej ją u siebie.

**Mam ten sam model na dwóch komputerach i chcę, żeby to miało sens.**
Zakładka LLM → zaznacz ten model na obu hostach (checkboxy w sekcji
"Modele wystawione") → zapisz wybór. Panel od razu pokaże
"zbalansowany na 2 hostach" — LiteLLM sam rozkłada zapytania. Opcjonalnie
możesz jeszcze ustawić priorytet (np. szybszy komputer jako "1") albo
fallback na inny, mniejszy model, gdyby oba hosty akurat zawiodły.

**Host przestał odpowiadać.**
Sprawdź jego status w zakładce Master. Jeśli śpi lub jest wyłączony —
zakładka Slave → Wake-on-LAN. Jeśli działa, ale się zawiesił — zdalny restart
z tej samej zakładki.

**Zmieniłem ustawienia, ale nic się nie dzieje.**
Zmiany wykonuje demon (`octollama-daemon`). Jeśli jest zatrzymany,
polecenia z panelu czekają w `state.json` do jego uruchomienia. Sprawdź:
`sudo systemctl status octollama-daemon` (na hoście, którego dotyczy zmiana).

---

## 4. Bezpieczeństwo — co warto wiedzieć

- Panel wymaga logowania (jeden użytkownik, hash hasła w lokalnym pliku, bez bazy).
- Demon rootowy nie nasłuchuje na żadnym porcie — komunikacja tylko przez pliki.
- Eksporty NFS są ograniczone per adres IP hosta.
- Panel nie ma TLS — jeśli chcesz HTTPS, postaw przed nim reverse proxy
  (np. Nginx Proxy Manager). To świadoma decyzja projektowa.
- OctoLlama jest przeznaczona do **sieci domowej / zaufanej** — nie wystawiaj
  portu 5000 ani 4000 bezpośrednio do internetu.
