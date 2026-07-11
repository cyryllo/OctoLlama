"""Cienka nakładka na REST API Ollamy — lista/pobieranie/usuwanie modeli.

Działa na DOWOLNYM hoście (master i każdy slave mają ten sam `/api/...`) —
zero roota potrzebne, identycznie jak dziś steruje modelami Ollama Manager
(klasa `OllamaClient` w ~/Projekty/Ollama-manager/ollama_manager.py), tylko
`base_url` zamiast stałego adresu.
"""

import json

import requests


class OllamaClient:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def api_dziala(self):
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=2)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def list_models(self):
        # WHY: pełne wpisy (nie tylko nazwa) - panel WWW pokazuje też rozmiar.
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
            return r.json().get("models", [])
        except requests.RequestException:
            return []

    def list_loaded(self):
        # WHAT: modele aktualnie załadowane do pamięci (RAM/VRAM), z /api/ps.
        try:
            r = requests.get(f"{self.base_url}/api/ps", timeout=5)
            r.raise_for_status()
            return r.json().get("models", [])
        except requests.RequestException:
            return []

    def delete_model(self, name):
        r = requests.delete(f"{self.base_url}/api/delete", json={"model": name}, timeout=30)
        r.raise_for_status()

    def pull_stream(self, model):
        # WHAT: generator - oddaje kolejne komunikaty postępu pobierania.
        with requests.post(
            f"{self.base_url}/api/pull",
            json={"model": model, "stream": True},
            stream=True,
            timeout=None,  # WHY: pobranie kilku GB trwa - brak limitu czasu
        ) as r:
            r.raise_for_status()
            for linia in r.iter_lines():
                if linia:
                    yield json.loads(linia)
