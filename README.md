# relocarr

Hands finished Radarr and Sonarr imports over to your NAS or custom layout, then cleans up the
download client. See [PLAN.md](PLAN.md) for the full design and the rationale behind it.

**Status: v0.1 feature-complete, not yet run against production with dry-run off.** Every piece
(webhook receiver, job state machine, removal gate, download-client watcher, scheduler, Preflight
page, Docker image) is implemented and tested — see PLAN.md's "v0.1 implementation status" under
§15 for exactly what's been verified and what the recommended first real run looks like.

## Quick start

1. Add a Radarr or Sonarr instance through the setup wizard (Add App).
2. On its App Detail page, set the target folder, layout, and a Transmission download client.
3. Check its Preflight page — it audits the *arr's own settings for the things that would let
   two automations fight over the same files (see PLAN.md §10).
4. Add the Webhook connection it shows you to the *arr (Connect → Webhook).
5. Leave **dry run** on (Settings — it's on by default) and watch the logs for what it *would*
   do on a real import before turning it off.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
RELOCARR_CONFIG_DIR=./config uvicorn relocarr.main:app --reload --port 8686 --app-dir src
```

## Docker

```bash
docker build -t relocarr .
docker run -d -p 8686:8686 -e PUID=1000 -e PGID=1000 \
  -v ./config:/config \
  -v /downloads:/downloads \
  -v /mnt/media:/mnt/media \
  relocarr
```

Runs as a non-root user derived from `PUID`/`PGID` (default 1000:1000). See PLAN.md §17 for a
fuller reference deployment.

## License

MIT
