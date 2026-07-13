"""Globalne ustawienia routingu LiteLLM (strategia, retry/timeout/cooldown,
fallbacki, priorytet hostów, context window fallbacks) - zakładka "LLM".

Osobny plik od hosts.json, bo to NIE jest własność żadnego hosta z osobna,
tylko globalna konfiguracja agregatora - ten sam wzorzec przechowywania co
hosts_store.py (JSON obok kodu, zapis atomowy tmp+rename, override ścieżki
przez zmienną środowiskową do testów).
"""

import json
import os
from pathlib import Path

USTAWIENIA_PATH = Path(
    os.environ.get(
        "OCTOLLAMA_LITELLM_USTAWIENIA_FILE",
        Path(__file__).parent / "litellm_ustawienia.json",
    )
)

DOMYSLNE = {
    "routing_strategy": "simple-shuffle",
    "num_retries": 2,
    "timeout": 600,
    "cooldown_time": 60,
    "allowed_fails": 3,
    "fallbacks": {},
    "context_window_fallbacks_wlaczone": False,
    "context_window_fallbacks": {},
    "priorytet": {},
    "role_modele": {},
}

STRATEGIE_ROUTINGU = (
    "simple-shuffle",
    "least-busy",
    "latency-based-routing",
    "usage-based-routing-v2",
)

# WHY: role, jakie Continue.dev rozumie w polu `roles` configu modelu - patrz
# litellm_manager._role_domyslne oraz zbuduj_config_continue. Kolejność tu
# to kolejność wyświetlania checkboxów w zakładce LLM.
ROLE_CONTINUE = ("chat", "autocomplete", "edit", "apply", "embed", "rerank")

# WHY: role oparte o function-calling - model bez "tools" w capabilities
# (patrz litellm_manager.role_dla_modelu) dostaje w Continue twarde
# "does not support tools" za każdym razem, gdy się je wywoła, więc w ogóle
# nie pozwalamy ich zaznaczyć dla takiego modelu (checkbox disabled w UI +
# odfiltrowane po stronie serwera, patrz app.llm_zapisz_role).
ROLE_WYMAGA_TOOLS = frozenset({"edit", "apply"})

OPISY_ROL = {
    "chat": "Rozmowa w czacie Continue.",
    "autocomplete": "Podpowiedzi inline w edytorze (wymaga wsparcia FIM/insert po stronie modelu).",
    "edit": "Edycja zaznaczonego fragmentu kodu poleceniem.",
    "apply": "Nakładanie zaproponowanych przez czat zmian na plik.",
    "embed": "Model embeddingowy używany w RAG (przeszukiwanie kontekstu) - nie do rozmowy.",
    "rerank": "Ranking/przeszukiwanie wyników wyszukiwania kontekstu.",
}

# WHY: teksty źródłowe (polski) do przetłumaczenia przez _() DOPIERO w
# szablonie/przy renderowaniu (zależnie od sesji usera) - stąd zwykłe stringi
# tutaj, nie wywołanie _() na poziomie modułu (import następuje raz, poza
# kontekstem requestu/sesji Flaska).
OPISY_STRATEGII = {
    "simple-shuffle": "Losowy wybór hosta przy każdym zapytaniu (najprostsze, domyślne).",
    "least-busy": "Wybiera host, który aktualnie przetwarza najmniej zapytań.",
    "latency-based-routing": "Wybiera host o najniższym zmierzonym opóźnieniu odpowiedzi.",
    "usage-based-routing-v2": "Uwzględnia limity zużycia (tokeny/zapytania na minutę) przy wyborze hosta.",
}


def wczytaj_ustawienia():
    try:
        zapisane = json.loads(USTAWIENIA_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        zapisane = {}
    # WHY: merge z domyślnymi, nie zwykłe .get() na brakującym pliku - żeby
    # dodanie nowego pola w przyszłości (np. kolejna opcja routingu) nie
    # wymagało migracji istniejących plików ustawień na dysku.
    return {**DOMYSLNE, **zapisane}


def zapisz_ustawienia(ustawienia):
    tmp = USTAWIENIA_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(ustawienia, indent=2, ensure_ascii=False))
    tmp.rename(USTAWIENIA_PATH)
