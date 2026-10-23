# relocarr

Hands finished Radarr and Sonarr imports over to your NAS or custom layout, then cleans up the
download client. See [PLAN.md](PLAN.md) for the full design and the rationale behind it.

**Status: v0.1.0 released, verified end-to-end against production.** Every piece (webhook
receiver, job state machine, removal gate, download-client watcher, scheduler, Preflight page,
Docker image) is implemented, tested, and has moved real files through the full pipeline —
see PLAN.md's "v0.1 implementation status" under §15 for details.

## Screenshots

| Log | App Config |
|---|---|
| ![Transfer log](docs/screenshots/log.png) | ![App configuration](docs/screenshots/app-config.png) |

| Download Clients | System |
|---|---|
| ![Download clients](docs/screenshots/download-clients.png) | ![System status](docs/screenshots/system.png) |

## Install

Requires Docker + the Docker Compose plugin.

```bash
git clone https://github.com/a000a999a/relocarr.git
cd relocarr
./install.sh
```

The script asks a few questions (your downloads/staging root, your media/target root, and
optionally the Docker network your Radarr/Sonarr/Transmission containers are already on), writes
them to `.env`, then builds and starts the container.

- **Re-run it any time** — with no flags it reuses your existing `.env`, rebuilds the image, and
  restarts the container without touching your configured apps or job history.
- `./install.sh --reconfigure` — change the paths, network, port, etc. (backs up the old `.env`
  first).
- `./install.sh --reset-db` — wipe all configured apps/jobs and start over from an empty database
  (backs up the old one first). Useful for a true "configure from scratch."

The two root paths **must be the same path your *arr apps themselves use** — relocarr has to see
each file at the exact path the *arr reports it at in the webhook payload. See `.env.example`
for the full list of settings.

## Configure

1. Open the web UI (the install script prints the URL, default `http://<this host>:8686/`) and
   add a Radarr or Sonarr instance through the setup wizard (Add App).
2. On its App Detail page, set the target folder, layout, and a Transmission download client.
3. Check its Preflight page — it audits the *arr's own settings for the things that would let
   two automations fight over the same files (see PLAN.md §10).
4. Add the Webhook connection it shows you to the *arr (Connect → Webhook).
5. Leave **dry run** on (Settings — it's on by default) and watch the logs for what it *would*
   do on a real import before turning it off.

See the in-app **Setup Guide** page for the full Radarr/Sonarr/Transmission checklist.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
RELOCARR_CONFIG_DIR=./config uvicorn relocarr.main:app --reload --port 8686 --app-dir src
```

## License

MIT
