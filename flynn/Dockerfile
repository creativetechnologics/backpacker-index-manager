FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FILL_HOST=0.0.0.0 \
    FILL_PORT=8080 \
    BACKPACKER_SUPPORT_DIR=/var/lib/backpacker-index-manager

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# The wikivoyage_dump/ directory is the Python package. Copy it as
# the WHOLE tree and then install deps.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY wikivoyage_dump/ /app/wikivoyage_dump/
# Install the staging DB's psql client into the image (already
# listed in the apt-get install above) so the lane worker can pipe
# SQL into the staging-db container over the flynn_mesh network.

# Persistent state directory for lanes.json, api-keys.json, fill_state.jsonl
RUN mkdir -p /var/lib/backpacker-index-manager

WORKDIR /app/wikivoyage_dump

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=15s \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# Disable browser auto-open inside Docker; the dashboard is reached
# via the Flynn-launched port, not a desktop browser.
ENV FILL_NO_BROWSER=1

CMD ["python3", "-u", "initial_database_fill.py", "--host", "0.0.0.0", "--port", "8080"]
