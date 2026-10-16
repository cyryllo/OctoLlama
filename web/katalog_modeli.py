"""Wyszukiwanie modeli do pobrania — JEDNA wyszukiwarka dla dwóch źródeł:
oficjalnej biblioteki Ollamy (ollama.com) i Hugging Face (tylko GGUF - reszta HF
jest w surowym safetensors, tego Ollama nie konwertuje w locie; Ollama pobiera
GGUF z HF przez 'hf.co/<user>/<repo>:<kwantyzacja>').

Port funkcji z Ollama Managera (~/Projekty/Ollama-manager/ollama_manager.py,
sekcja "Wyszukiwanie i pobieranie modeli") - łącznie z poprawką parsera po
zmianie klas CSS na ollama.com (2026-10). Te funkcje tylko ZNAJDUJĄ modele/
warianty i dają gotową nazwę do pobrania - samo pobieranie idzie istniejącym
pobierania.rozpocznij() (patrz modele.html).
"""

import html
import re
from pathlib import Path

import requests

from i18n import przetlumacz as _

# WHAT: znane PRZEDROSTKI rodzin kwantyzacji GGUF (nazewnictwo llama.cpp/ggml)
#       - regex dopasowuje przedrostek + resztę tego samego "słowa", więc cały
#       token typu "Q4_K_M", "Q3_K_XL" czy "Q4_0_4_4" (ARM repack) to JEDNA
#       kwantyzacja. WHY: sztywna lista pełnych etykiet sklejała np. Q4_0_4_4
#       z Q4_0 i sumowała rozmiary (bug znaleziony w Ollama Managerze).
_GGUF_KWANTYZACJA_PREFIKSY = [
    "IQ1", "IQ2", "IQ3", "IQ4",
    "TQ1", "TQ2",
    "Q2", "Q3", "Q4", "Q5", "Q6", "Q8",
    "BF16", "FP16", "F16", "FP32", "F32",
]
_GGUF_KWANTYZACJA_WZORZEC = re.compile(
    r"(?<![A-Za-z0-9_])(?:" + "|".join(_GGUF_KWANTYZACJA_PREFIKSY) + r")[A-Za-z0-9_]*",
    re.IGNORECASE,
)

# WHAT: pliki .gguf, które NIE są wariantem modelu: projektor obrazu (mmproj),
#       macierz ważności (imatrix) i głowice MTP do dekodowania spekulacyjnego.
# BUG naprawiony: dawniej mmproj-BF16.gguf (0,9 GB) pokazywał się jako wariant
#       "BF16" modelu 27B, a MTP/mtp-...-Q4_0.gguf doliczał się do rozmiaru Q4_0
#       (sprawdzone na unsloth/Qwen3.8-27B-GGUF).
_PLIK_POMOCNICZY_GGUF = re.compile(r"(^|[/_-])(mmproj|imatrix|mtp)([/_.-]|$)", re.IGNORECASE)


def hf_repo_z_tekstu(tekst):
    # WHAT: wyciąga "użytkownik/repo" z linku do Hugging Face, "użytkownik/repo"
    #       albo "hf.co/użytkownik/repo:kwant". None, gdy to nie wygląda na repo.
    tekst = tekst.strip()
    tekst = re.sub(r"^(https?://)?(www\.)?(huggingface\.co|hf\.co)/", "", tekst, flags=re.IGNORECASE)
    czesci = [c for c in tekst.strip("/").split("/") if c]
    # WHY: link do pliku/gałęzi (".../blob/main/plik.gguf") ma więcej segmentów
    #      - samo repo to zawsze pierwsze dwa. Tag obcinamy dopiero z GOTOWEGO
    #      repo (obcięcie po ":" z całego tekstu zostawiało z "https://" "https").
    if len(czesci) < 2:
        return None
    return "/".join(czesci[:2]).split(":", 1)[0]


def zrodlo_modelu(nazwa):
    # WHAT: skąd pochodzi zainstalowany model - po samej nazwie.
    return "Hugging Face" if nazwa.startswith("hf.co/") else "Ollama"


def _rozmiar_na_bajty(tekst):
    # WHAT: "4.7GB"/"398MB" (format ollama.com) -> bajty; None gdy nieczytelne.
    dopasowanie = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?B)\s*", tekst or "", re.IGNORECASE)
    if not dopasowanie:
        return None
    mnozniki = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    return int(float(dopasowanie.group(1)) * mnozniki[dopasowanie.group(2).upper()])


def _formatuj_liczbe(n):
    # WHAT: liczba pobrań w skróconej formie ("21.8M") - tak jak ollama.com,
    #       żeby oba źródła w tabeli wyników wyglądały spójnie.
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def ollama_wyszukaj_modele(zapytanie):
    # WHAT: szuka modeli w bibliotece Ollamy (ollama.com/search) - nazwa, opis,
    #       możliwości, rozmiary, liczba pobrań.
    # WHY:  ollama.com NIE ma publicznego API wyszukiwania - jedynym źródłem jest
    #       strona HTML. Parser nie zależy od klas CSS linku (zmieniły się
    #       2026-10: "group w-full" -> "group flex ..."): blok wyniku to link do
    #       /library/<nazwa> zawierający nagłówek <h2>.
    # UWAGA: celowo KRUCHE miejsce - przy kolejnej zmianie wyglądu strony
    #        zgłaszamy błąd zamiast po cichu pokazywać same wyniki z HF.
    r = requests.get("https://ollama.com/search", params={"q": zapytanie}, timeout=10)
    r.raise_for_status()
    czesci = re.split(r'<a href="/library/([^"/:?#]+)"[^>]*>', r.text)
    wyniki = []
    for i in range(1, len(czesci), 2):
        blok = czesci[i + 1].split("</a>", 1)[0]
        if "<h2" not in blok:
            continue
        rozmiary = re.findall(r'bg-\[#ddf4ff\][^>]*>\s*([^<]+?)\s*<', blok)
        # WHY: modele TYLKO w chmurze Ollamy (znaczek "cloud", brak rozmiarów)
        #      nie dadzą się pobrać - pomijamy je.
        if not rozmiary and re.search(r">\s*cloud\s*<", blok):
            continue
        opis = re.search(r"<p[^>]*>(.*?)</p>", blok, re.S)
        dokladnie = re.search(r'title="([\d,]+) (?:downloads|pulls)"', blok, re.IGNORECASE)
        if dokladnie:
            pobrania = _formatuj_liczbe(int(dokladnie.group(1).replace(",", "")))
        else:
            stary = re.search(r'>\s*([\d.,]+[KMB]?)\s*</span>\s*<span[^>]*>&nbsp;Pulls', blok)
            pobrania = stary.group(1) if stary else ""
        wyniki.append({
            "zrodlo": "ollama",
            "id": czesci[i],
            "opis": html.unescape(re.sub(r"<[^>]+>|\s+", " ", opis.group(1))).strip() if opis else "",
            "mozliwosci": re.findall(r'bg-indigo-50[^>]*>\s*([^<]+?)\s*<', blok),
            "rozmiary": rozmiary,
            "pobrania": pobrania,
        })
    if not wyniki and "<h2" in r.text and 'href="/library/' in r.text and ">cloud<" not in r.text.replace(" ", ""):
        raise ValueError(_("Nie udało się odczytać wyników z ollama.com - strona mogła zmienić wygląd."))
    return wyniki


def ollama_warianty_modelu(model):
    # WHAT: czyta stronę tagów (ollama.com/library/<model>/tags) i zwraca listę
    #       (tag, rozmiar_bajty, kontekst). Pomija MLX (tylko Apple Silicon) i
    #       tagi "cloud"/bez rozmiaru (nie do pobrania lokalnie).
    r = requests.get(f"https://ollama.com/library/{model}/tags", timeout=10)
    if r.status_code == 404:
        raise ValueError(_("Nie znaleziono takiego modelu w bibliotece Ollamy."))
    r.raise_for_status()
    wiersze = re.findall(
        r'<a href="/library/' + re.escape(model) + r':([^"]+)" class="md:hidden[^"]*">(.*?)</a>',
        r.text, re.S,
    )
    warianty = []
    for tag, blok in wiersze:
        tekst = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", blok))
        if "cloud" in tag or re.search(r"\bMLX\b", tekst):
            continue
        rozmiar = re.search(r"•\s*([\d.]+\s*[KMGT]?B)\s*•", tekst)
        if not rozmiar:
            continue
        kontekst = re.search(r"•\s*(\d+[KM]?)\s+context window", tekst)
        warianty.append((f"{model}:{tag}", tag, _rozmiar_na_bajty(rozmiar.group(1)),
                         kontekst.group(1) if kontekst else ""))
    if not warianty:
        raise ValueError(_("Ten model nie ma wariantów do pobrania na Linuksie (tylko MLX albo cloud)."))
    return warianty


def hf_wyszukaj_repozytoria(zapytanie):
    # WHAT: repo na HF pasujące do zapytania, tylko z tagiem 'gguf' (HF dokleja
    #       go sam, gdy wykryje pliki .gguf), najpopularniejsze pierwsze.
    r = requests.get(
        "https://huggingface.co/api/models",
        params={"search": zapytanie, "filter": "gguf", "sort": "downloads", "direction": "-1", "limit": 20},
        timeout=10,
    )
    r.raise_for_status()
    return [
        {"zrodlo": "hf", "id": wpis["id"], "opis": "", "mozliwosci": [], "rozmiary": [],
         "pobrania": _formatuj_liczbe(wpis.get("downloads", 0))}
        for wpis in r.json() if wpis.get("id")
    ]


def hf_warianty_gguf(repo):
    # WHAT: pliki .gguf w repo (endpoint 'tree' zwraca rozmiar każdego pliku),
    #       pogrupowane wg kwantyzacji -> (nazwa_do_pull, kwant, rozmiar, "").
    # WHY:  modele dzielone na części ("-00001-of-00002") sumujemy w jeden wpis
    #       (Ollama sama złoży części pod jednym tagiem), ale gdy repo ma TEN SAM
    #       model też w całości - liczy się tylko plik w całości.
    r = requests.get(f"https://huggingface.co/api/models/{repo}/tree/main",
                     params={"recursive": "true"}, timeout=10)
    if r.status_code in (401, 404):
        raise ValueError(_("Nie znaleziono takiego repozytorium na Hugging Face."))
    r.raise_for_status()

    calosci, czesci = {}, {}
    for wpis in r.json():
        sciezka = wpis.get("path", "")
        if not sciezka.lower().endswith(".gguf") or _PLIK_POMOCNICZY_GGUF.search(sciezka):
            continue
        stem = Path(sciezka).stem
        dopasowanie = _GGUF_KWANTYZACJA_WZORZEC.search(stem)
        kwant = dopasowanie.group(0).upper() if dopasowanie else None
        cel = czesci if re.search(r"-\d{5}-of-\d{5}$", stem) else calosci
        cel[kwant] = cel.get(kwant, 0) + wpis.get("size", 0)

    warianty = {**czesci, **calosci}
    if not warianty:
        raise ValueError(_("To repozytorium nie zawiera żadnych plików .gguf."))
    # WHY: bez rozpoznanej kwantyzacji (repo z jednym plikiem .gguf bez dopisku)
    #      Ollama chce sam 'hf.co/user/repo', bez dwukropka - taki wpis na końcu.
    return [
        (f"hf.co/{repo}:{kwant}" if kwant else f"hf.co/{repo}", kwant, rozmiar, "")
        for kwant, rozmiar in sorted(warianty.items(), key=lambda kv: (kv[0] is None, kv[0] or ""))
    ]


def szukaj(zapytanie):
    # WHAT: (wyniki, błędy) z obu źródeł. Tekst z "/" (link do HF, "user/repo",
    #       "hf.co/...") to od razu jedno repo HF, bez szukania.
    # WHY:  błąd jednego źródła (brak sieci do HF, zmiana strony ollama.com) NIE
    #       może ukryć wyników z drugiego. Ollama zawsze na górze listy.
    if "/" in zapytanie:
        repo = hf_repo_z_tekstu(zapytanie)
        if repo:
            return [{"zrodlo": "hf", "id": repo, "opis": "", "mozliwosci": [], "rozmiary": [], "pobrania": ""}], []
    wyniki, bledy = [], []
    try:
        wyniki += ollama_wyszukaj_modele(zapytanie)
    except (requests.RequestException, ValueError) as e:
        bledy.append(f"Ollama: {e}")
    try:
        wyniki += hf_wyszukaj_repozytoria(zapytanie)
    except requests.RequestException as e:
        bledy.append(f"Hugging Face: {e}")
    return wyniki, bledy


def warianty(zrodlo, model_id):
    # WHAT: lista (nazwa_do_pull, etykieta, rozmiar_bajty, kontekst) dla wyniku.
    # WHY:  model_id przychodzi z parametru URL i trafia do ścieżki zapytania
    #       do ollama.com/HF - dopuszczamy tylko kształt prawdziwej nazwy.
    wzorzec = r"[\w.-]+/[\w.-]+" if zrodlo == "hf" else r"[\w.-]+"
    if not re.fullmatch(wzorzec, model_id or "") or ".." in model_id:
        raise ValueError(_("Nieprawidłowa nazwa modelu."))
    if zrodlo == "hf":
        return hf_warianty_gguf(model_id)
    return ollama_warianty_modelu(model_id)
