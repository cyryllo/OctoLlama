# OctoLlama

<p align="center"><img src="logo_octollama.png" alt="OctoLlama" width="600"></p>

A web panel for managing [Ollama](https://ollama.com), the
[LiteLLM](https://www.litellm.ai/) aggregator, and [Open WebUI](https://openwebui.com/)
across multiple hosts on your home network — no desktop app, with login, reachable
from a browser (including your phone). The web-based counterpart to the
[Ollama Manager](https://github.com/cyryllo/Ollama-manager) desktop app (PyQt6/KDE).

*(Polska wersja tego pliku: [README_PL.md](README_PL.md))*

## Features

- **Ollama service control** — start/stop/autostart, install on demand, every
  environment variable that affects performance (context size, VRAM, Vulkan/iGPU,
  KV cache, network availability).
- **Model management** — list, size, what's currently loaded in memory, pulling
  new models with a progress bar, deleting.
- **LiteLLM aggregator** — a single OpenAI-compatible endpoint over models from
  every host at once; a deliberate choice of which models get exposed.
- **Open WebUI** — a chat panel wired to LiteLLM (not to a single host), so it
  sees exactly the models you chose to expose.
- **Continue.dev config** (VS Code) — generated from the currently exposed
  models, for manual pasting (the panel never overwrites the user's config).
- **Multi-host** — add remote hosts (e.g. a mini-PC running Ollama) with an
  auto-generated installer for the new machine.
- **Host power management** — Wake-on-LAN to wake a sleeping/off host, plus
  remote power off/restart/suspend.
- **Login** — single user, password hash in a local file, no database.
- **Multi-language** — Polish and English (switcher in the header), easy to
  extend with more languages.

## How it works

Changing systemd service state (start/stop, environment variables) requires
root. Instead of giving the web panel root, each host runs two separate
processes:

```
┌─────────────────────────────┐         ┌──────────────────────────────┐
│  ollama-manager-web         │         │  ollama-manager-daemon        │
│  (user, NO root)            │         │  (root, systemd system unit)  │
│                              │         │                                │
│  - web panel + login        │         │  - inotify on the state file   │
│  - model operations         │  state  │  - diff: what changed           │
│    -> straight to /api/...  │  file   │  - override.conf + reload/     │
│    (no root needed)         │ ──────► │    restart/enable/disable       │
│  - writes "what the user    │ (JSON)  │  - writes status.json with      │
│    wants" to the state file │ ◄────── │    the result (OK/error)        │
└─────────────────────────────┘ status  └──────────────────────────────┘
                                 .json
```

The web panel never calls `systemctl` directly — it writes the desired state to
`state.json`, and a local root-owned daemon applies it and reports the result in
`status.json`. The only contact root has with the outside world is a file on
disk, zero network port. Operations that don't need root (models through
`/api/...`, LiteLLM through `systemd --user`) go straight from the panel.

Adding a remote host (e.g. a mini-PC) works similarly: the management host is
an NFS server, exports a separate directory per host (restricted to its IP),
and the panel generates a ready-to-run install script with the daemon's code
baked in — run once, manually, on the new machine.

## Requirements

- Linux with `systemd` (Debian/Ubuntu — the installer uses `apt`).
- Python 3.11+.
- `sudo` privileges (for installing the daemon, NFS, system dependencies).

## Installation

On the host that will be the "master" (the one running the web panel):

```bash
git clone git@github.com:cyryllo/OctoLlama.git
cd OctoLlama
./install.sh
```

The script installs (skipping anything already installed):

1. Ollama (official installer from ollama.com),
2. LiteLLM (`uv tool install litellm[proxy]`, no root),
3. Open WebUI (`uv tool install --python 3.11 open-webui`, no root — just the
   binary, starting it is left to the button in the panel's WebUI tab),
4. `nfs-kernel-server` (to support remote hosts),
5. `ollama-manager-daemon` — a system `systemd` service (root),
6. `ollama-manager-web` — a `systemd --user` service, the web panel on port 5000.

On first install, the script asks you to set your login username/password
right there (password entered twice, to confirm). To change credentials
later, run it again by hand:

```bash
cd ~/.local/share/ollama-manager-web
./.venv/bin/python3 manage_users.py
```

The panel will be available at `http://<this-host's-address>:5000`.

### Updating

`git pull` then re-run `./install.sh`. It detects the installed version (from
the `VERSION` file) against the one in your checkout:

- **older installed** — offers to update (default: yes),
- **same version** — asks before reinstalling (default: no),
- **newer installed** — warns before overwriting it with an older one.

### Uninstalling

```bash
./install.sh --uninstall
```

Always removes OctoLlama's own daemon and web panel (services, units,
installed files), then asks separately — each defaulting to **no** — whether
to also remove: the state directory and NFS exports for remote hosts, LiteLLM,
Open WebUI, Ollama (**this deletes every downloaded model**), and
`nfs-kernel-server`. Nothing beyond OctoLlama itself is removed unless you say
yes.

## Managing the services

Two independent services get installed — the daemon (system, root) and the web
panel (user, no root):

```bash
# ollama-manager-daemon (root, system service)
sudo systemctl status ollama-manager-daemon
sudo systemctl restart ollama-manager-daemon
sudo systemctl stop ollama-manager-daemon
sudo systemctl start ollama-manager-daemon
sudo journalctl -u ollama-manager-daemon -f      # live log

# ollama-manager-web (user service, no sudo)
systemctl --user status ollama-manager-web
systemctl --user restart ollama-manager-web
systemctl --user stop ollama-manager-web
systemctl --user start ollama-manager-web
journalctl --user -u ollama-manager-web -f       # live log
```

Restarting the web panel does **not** affect the Ollama service itself — the
daemon keeps enforcing the last state it received from `state.json`
regardless of whether the panel is running. Stopping the daemon means changes
made in the panel (start/stop/env variables) won't be applied until it's
running again.

## Usage

The panel has four tabs:

- **Master** — the Ollama service and its environment variables on this host,
  the status of every connected host, a link to manage models.
- **Slave** — add/remove remote Ollama hosts. After adding a host, the panel
  generates `install-<name>.sh` — download and run it (over SSH) on the target
  machine; it installs Ollama, mounts the state directory over NFS, and sets up
  its own daemon. Also Wake-on-LAN (the MAC address is auto-detected from the
  host's ARP entry when it's reachable, or can be entered manually) and remote
  power off/restart/suspend per host.
- **LLM** — start/stop the LiteLLM aggregator, choose which models from which
  hosts get exposed, generate the Continue.dev config.
- **WebUI** — start/stop Open WebUI, wired to LiteLLM (sees the same,
  deliberately selected models, from every host at once).

## Repo layout

```
daemon/
  ollama_manager_daemon.py   Root-owned daemon (systemd system unit)
  systemd/                   systemd unit for the daemon
web/
  app.py                     Routes / view logic (Flask)
  ollama_client.py           Ollama REST API client (models)
  litellm_manager.py         LiteLLM control + Continue.dev config
  openwebui_manager.py       Open WebUI control (wired to LiteLLM)
  hosts_store.py             Host list (Slave tab)
  install_generator.py       Installer generator for remote hosts
  wol.py                     Wake-on-LAN (magic packet)
  state_store.py             state.json / status.json read/write
  pobierania.py              Background model-pull progress tracking
  i18n.py                    Translations (_(), language choice in session)
  lang/en.json               English translation dictionary
  templates/                 Jinja templates
  static/style.css           Styles (light/dark theme)
install.sh                   Installer for the management host
```

## Adding another language

Same pattern as [Ollama Manager](https://github.com/cyryllo/Ollama-manager)
(`lang/*.json` keyed by the Polish source text): create `web/lang/<code>.json`
with translations, add `"<code>": "name"` to `JEZYKI` in `web/i18n.py`. A
missing dictionary entry = the panel shows the original (Polish), so an
incomplete translation never leaves a blank spot.

## Status

A working skeleton — service control, models, LiteLLM, Open WebUI, Continue.dev
config, multi-host support (NFS + installer generator), and host power
management (Wake-on-LAN + remote power off/restart/suspend) all work
end-to-end. The NFS/remote-daemon path hasn't been verified on real hardware
yet. Deliberately left out: TLS (to be handled by a reverse proxy in front of
the panel).

## Related projects

- [Ollama Manager](https://github.com/cyryllo/Ollama-manager) — the PyQt6/KDE
  desktop app, the source of the service/model/LiteLLM control logic ported
  here.

## License

[GNU GPLv3](LICENSE) — this project contains logic ported from
[Ollama Manager](https://github.com/cyryllo/Ollama-manager) (GPLv3), so it
inherits the same license.
