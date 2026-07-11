#!/bin/bash
# =============================================================================
#  Instalator dla HOSTA ZARZĄDZAJĄCEGO (tego, na którym ma stać panel WWW).
#
#  Za jednym razem instaluje/aktualizuje WSZYSTKO, co ten host potrzebuje:
#    1) Ollama (oficjalny installer ollama.com)
#    2) LiteLLM (agregator-gateway, jako `uv tool install`, bez roota)
#    3) nfs-kernel-server - serwer eksportów dla zdalnych hostów (BC-250 itd.),
#       same eksporty per-host zarządza już potem demon (zakładka Slave)
#    4) ollama-manager-daemon (root, system systemd unit) - steruje lokalną
#       usługą Ollama WYŁĄCZNIE przez state.json (patrz README.md, Architektura)
#    5) ollama-manager-web (bez roota, systemd --user) - panel WWW logowania
#
#  Dla ZDALNEGO hosta (np. BC-250), który ma tylko Ollamę + demona, a NIE panel
#  WWW ani LiteLLM - użyj skryptu generowanego przez sam panel WWW
#  (README.md, sekcja "Wielohostowość"), nie tego pliku.
#
#  Uruchom:  ./install.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
DAEMON_SRC="$SCRIPT_DIR/daemon"
WEB_SRC="$SCRIPT_DIR/web"

DAEMON_DIR="/opt/ollama-manager/daemon"
STATE_DIR="/var/lib/ollama-manager/state"
DAEMON_UNIT_DST="/etc/systemd/system/ollama-manager-daemon.service"

WEB_DIR="$HOME/.local/share/ollama-manager-web"
WEB_UNIT_DST="$HOME/.config/systemd/user/ollama-manager-web.service"

echo "=== [1/5] Ollama ==="
if command -v ollama >/dev/null 2>&1; then
    echo "Już zainstalowana - pomijam."
else
    echo "Instaluję (oficjalny installer z ollama.com, może zapytać o hasło sudo)..."
    curl -fsSL https://ollama.com/install.sh | sh
fi

echo
echo "=== [2/5] LiteLLM ==="
_uv_binarka() {
    command -v uv 2>/dev/null && return 0
    [ -x "$HOME/.local/bin/uv" ] && { echo "$HOME/.local/bin/uv"; return 0; }
    return 1
}
if UV="$(_uv_binarka)"; then
    :
else
    echo "Instaluję uv (do zarządzania narzędziami Pythona bez psucia systemowego pip)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    UV="$(_uv_binarka)"
fi
if command -v litellm >/dev/null 2>&1 || [ -x "$HOME/.local/bin/litellm" ]; then
    echo "Już zainstalowany - pomijam."
else
    echo "Instaluję LiteLLM..."
    "$UV" tool install 'litellm[proxy]'
fi

echo
echo "=== [3/5] Serwer NFS (eksporty dla zdalnych hostów) ==="
if command -v exportfs >/dev/null 2>&1; then
    echo "nfs-kernel-server już zainstalowany - pomijam."
else
    sudo apt-get install -y nfs-kernel-server
fi
sudo mkdir -p /srv/ollama-manager/hosts /etc/exports.d
sudo systemctl enable --now nfs-kernel-server

echo
echo "=== [4/5] ollama-manager-daemon (root, systemd system unit) ==="
sudo mkdir -p "$DAEMON_DIR"
sudo cp "$DAEMON_SRC/ollama_manager_daemon.py" "$DAEMON_SRC/requirements.txt" "$DAEMON_DIR/"
[ -d "$DAEMON_DIR/.venv" ] || sudo python3 -m venv "$DAEMON_DIR/.venv"
sudo "$DAEMON_DIR/.venv/bin/pip" install -q -r "$DAEMON_DIR/requirements.txt"
sudo mkdir -p "$STATE_DIR"
# WHY: demon (root) i tak zapisze wszędzie, ale panel WWW (zwykły user) też
# musi mieć prawo zapisu do state.json/status.json w tym katalogu.
sudo chown "$(id -u):$(id -g)" "$STATE_DIR"
sudo cp "$DAEMON_SRC/systemd/ollama-manager-daemon.service" "$DAEMON_UNIT_DST"
sudo systemctl daemon-reload
sudo systemctl enable ollama-manager-daemon.service
sudo systemctl restart ollama-manager-daemon.service

echo
echo "=== [5/5] ollama-manager-web (bez roota, systemd --user) ==="
mkdir -p "$WEB_DIR/templates" "$WEB_DIR/static"
cp "$WEB_SRC"/*.py "$WEB_SRC/requirements.txt" "$WEB_DIR/"
cp -r "$WEB_SRC/templates/." "$WEB_DIR/templates/"
cp -r "$WEB_SRC/static/." "$WEB_DIR/static/"
# WHY: hosts.json to konfiguracja usera (lista hostów Ollamy) - nie nadpisujemy
# przy ponownym uruchomieniu instalatora, jeśli user już go dostosował.
[ -f "$WEB_DIR/hosts.json" ] || cp "$WEB_SRC/hosts.json" "$WEB_DIR/"
[ -d "$WEB_DIR/.venv" ] || python3 -m venv "$WEB_DIR/.venv"
"$WEB_DIR/.venv/bin/pip" install -q -r "$WEB_DIR/requirements.txt"
mkdir -p "$(dirname "$WEB_UNIT_DST")"
cp "$WEB_SRC/systemd/ollama-manager-web.service" "$WEB_UNIT_DST"
systemctl --user daemon-reload
systemctl --user enable ollama-manager-web.service
systemctl --user restart ollama-manager-web.service

echo
echo "Gotowe."
if [ ! -f "$WEB_DIR/credentials.json" ]; then
    echo "Zanim zalogujesz się do panelu, ustaw dane logowania:"
    echo "  cd \"$WEB_DIR\" && ./.venv/bin/python3 manage_users.py"
fi
echo "Panel WWW: http://$(hostname -I 2>/dev/null | awk '{print $1}'):5000"
