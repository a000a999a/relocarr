# relocarr

Hands finished Radarr and Sonarr imports over to your NAS or custom layout, then cleans up the
download client. See [PLAN.md](PLAN.md) for the full design.

Status: early v0.1 development.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
RELOCARR_CONFIG_DIR=./config uvicorn relocarr.main:app --reload --port 8686 --app-dir src
```

## License

MIT
