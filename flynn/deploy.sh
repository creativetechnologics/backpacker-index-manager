#!/usr/bin/env bash
# Deploy the Backpacker Index Manager Python app to the Flynn Pi.
#
# Per Flynn rules, Docker runs on the Pi, not on this machine. We sync
# the relevant files (the Python source from wikivoyage_dump/ and the
# flynn/ app config) and rebuild there.
#
# Usage:
#   ./flynn/deploy.sh                  # deploy to gtbarnes@flynn.local
#   FLYNN_HOST=other ./flynn/deploy.sh
#
# Rollback: the previous /srv/apps/backpacker-index-manager/ is moved
# aside as a timestamped backup before the new one is copied in. To
# roll back: ssh to the Pi, restore the backup, and `docker compose up -d`.
set -euo pipefail

HOST="${FLYNN_HOST:-gtbarnes@flynn.local}"
REMOTE_APP_DIR="/srv/apps/backpacker-index-manager"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

echo "=== Backpacker Index Manager deploy ==="
echo "host:        $HOST"
echo "remote dir:  $REMOTE_APP_DIR"
echo "local root:  $ROOT"

# Stage the files to sync. We ship the Dockerfile + compose at the
# root of the remote app dir, and the Python source under
# wikivoyage_dump/ inside that.
STAGE="$(mktemp -d)"
trap "rm -rf '$STAGE'" EXIT

cp "$HERE/Dockerfile" "$STAGE/"
cp "$HERE/docker-compose.yml" "$STAGE/"
cp "$HERE/requirements.txt" "$STAGE/"
mkdir -p "$STAGE/wikivoyage_dump"
rsync -a --delete \
    --exclude='__pycache__/' \
    --exclude='.DS_Store' \
    "$ROOT/wikivoyage_dump/" "$STAGE/wikivoyage_dump/"

echo
echo "=== Staged files ==="
find "$STAGE" -maxdepth 2 -type f | sed "s|$STAGE|  |"

# A single timestamp for this deploy, used for the backup name.
TS="$(date +%Y%m%d-%H%M%S)"

echo
echo "=== Backing up existing app on the Pi (if any) ==="
# Use a heredoc with positional args to avoid nested-quote headaches.
ssh -4 "$HOST" bash -s -- "$REMOTE_APP_DIR" "$TS" <<'REMOTE_EOF'
set -e
REMOTE_DIR="$1"
TS="$2"

if [ -d "$REMOTE_DIR" ]; then
    if [ -f "$REMOTE_DIR/docker-compose.yml" ]; then
        if sudo docker ps -a --format '{{.Names}}' | grep -q '^flynn-backpacker-index-manager$'; then
            echo 'Stopping existing container...'
            sudo docker compose -f "$REMOTE_DIR/docker-compose.yml" down || true
        fi
        BACKUP="${REMOTE_DIR}.bak.${TS}"
        # If a previous deploy left a broken literal-name dir, nuke it.
        if [ -d "${REMOTE_DIR}.bak.\$ts" ]; then
            echo "Removing previous broken backup (literal \$ts dir)..."
            sudo rm -rf "${REMOTE_DIR}.bak.\$ts"
        fi
        echo "Moving old app to ${BACKUP}"
        sudo mv "$REMOTE_DIR" "$BACKUP"
    else
        echo "Existing dir is not an app — leaving it."
    fi
fi
sudo mkdir -p "$REMOTE_DIR"
REMOTE_EOF

echo
echo "=== Syncing new app to the Pi ==="
# Stream the staged tree over SSH, write into the app dir with sudo.
tar -C "$STAGE" -czf - . | ssh -4 "$HOST" "sudo tar -xzf - -C '$REMOTE_APP_DIR'"
ssh -4 "$HOST" "sudo chown -R gtbarnes:gtbarnes '$REMOTE_APP_DIR'"

echo
echo "=== Building and starting on the Pi ==="
ssh -4 "$HOST" bash -s -- "$REMOTE_APP_DIR" <<'REMOTE_EOF'
set -e
cd "$1"
sudo docker compose up -d --build
REMOTE_EOF

echo
echo "=== Verifying ==="
sleep 3
ssh -4 "$HOST" bash -s <<'REMOTE_EOF'
echo "--- container status ---"
sudo docker ps --filter 'name=flynn-backpacker-index-manager' --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
echo "--- /healthz ---"
curl -fsS http://127.0.0.1:8080/healthz || true
echo
echo "--- /flynn-app.json (head) ---"
curl -fsS http://127.0.0.1:8080/flynn-app.json | head -c 400
echo
echo "--- state file in the persistent volume ---"
# Find the actual prefixed volume name (compose prepends the project name).
VOL="$(sudo docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/backpacker-index-manager"}}{{.Name}}{{end}}{{end}}' flynn-backpacker-index-manager)"
echo "    volume name: $VOL"
sudo docker run --rm -v "$VOL:/data" alpine sh -c 'wc -l /data/fill_state.jsonl 2>/dev/null || echo "no state file yet"'
REMOTE_EOF

echo
echo "=== Done. Visit http://flynn.local:8497/ ==="
