# Backpacker Index Manager — Flynn App

This directory contains the Flynn application package for the Backpacker
Index fill pipeline. Once deployed, the Python web dashboard lives at
`http://flynn.local:8497/` and runs 24/7 on the Flynn Pi.

## Architecture

The app is a single Docker container built from this directory's
`Dockerfile`. It runs the Python web server from `../wikivoyage_dump/`
(the multi-lane fill system) on port 8080 inside the container. Flynn
publishes that port as `flynn.local:8497` via the existing `docker-compose.yml`.

State persists in a named Docker volume `backpacker-state`, mounted at
`/var/lib/backpacker-index-manager/` inside the container. Survives
container recreation and image rebuilds.

## Files

- `Dockerfile` — Python 3.12-slim image; installs deps; ships the
  `wikivoyage_dump/` source tree
- `docker-compose.yml` — service definition, `flynn.app.*` labels, named
  volume, `flynn_mesh` network
- `requirements.txt` — pinned Python dependencies (fastapi, uvicorn,
  sse-starlette, pydantic, httpx)
- `deploy.sh` — one-shot deploy: rsync, backup old app, rebuild, start

## Deploying

```bash
./flynn/deploy.sh
```

What it does:

1. Stages the relevant files in a temp dir (excludes `__pycache__`, `.DS_Store`)
2. SSHes to the Pi and **backs up** the existing app to
   `/srv/apps/backpacker-index-manager.bak.<timestamp>` (only if the
   existing dir is a real app — i.e. has a docker-compose.yml)
3. rsyncs the staged files into place
4. `docker compose up -d --build` on the Pi
5. Verifies `/healthz` and `/flynn-app.json`

To roll back:

```bash
ssh gtbarnes@flynn.local
sudo rm -rf /srv/apps/backpacker-index-manager
sudo mv /srv/apps/backpacker-index-manager.bak.<ts> /srv/apps/backpacker-index-manager
cd /srv/apps/backpacker-index-manager && sudo docker compose up -d
```

## Configuration

API keys and lane config are managed from the web dashboard at
`http://flynn.local:8497/` — no SSH needed for day-to-day use.

Initial setup after first deploy:

1. Open the dashboard
2. Configure tab → add your `openrouter`, `deepseek`, and `opencode-go`
   API keys
3. Edit lane `base_url` values to point at the right servers
4. Run tab → click **Start**

State files live in the `backpacker-state` Docker volume. They are
not on the host filesystem. To back them up:

```bash
ssh gtbarnes@flynn.local "sudo docker run --rm -v backpacker-state:/data -v \$(pwd):/backup alpine tar czf /backup/backpacker-state-\$(date +%Y%m%d).tgz -C /data ."
```

## Provider notes for the Pi

The `local` provider (oMLX) defaults to `http://localhost:8000/v1`,
which inside the container means the Pi itself, not your Mac. Edit the
`local-small` lane's **Base URL** in the Configure tab to point at your
Mac's LAN address (e.g. `http://garys-mac.local:8000/v1` or
`http://192.168.28.x:8000/v1`).

## Health and discovery endpoints

- `GET /healthz` — returns `{"ok": true, "state": "..."}` (200 if alive)
- `GET /flynn-app.json` — Flynn launcher manifest
- `GET /` — the dashboard
- `GET /api/...` — REST API for the dashboard
- `GET /events` — Server-Sent Events for live updates

## Local development

To test the container locally before deploying:

```bash
cd flynn
docker build -t backpacker-index-manager:dev .
docker run --rm -p 8742:8080 -v backpacker-state-dev:/var/lib/backpacker-index-manager backpacker-index-manager:dev
```

Then open `http://localhost:8742/`.
