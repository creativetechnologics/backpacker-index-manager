# Backpacker Index — Initial Fill (terminal-native)

Multi-lane pipeline that ingests Wikivoyage destination articles into the
staging database. Replaces the Swift-app-driven run flow with a standalone
Python CLI + web dashboard.

## Why this exists

The Swift app was acting as a remote control for a Python CLI. That had
three persistent bug classes: process lifecycle, IPC, signal handling. We
shipped 3 fixes and the prep-phase death was still unsolved. This tool
moves the orchestration out of Swift entirely.

## Run it

```bash
cd /Users/gary/Programming/Backpacker\ Index
python3 wikivoyage_dump/initial_database_fill.py
```

That starts:
- the web dashboard at `http://127.0.0.1:8742` (auto-opens)
- one subprocess per enabled lane

Press Ctrl-C to stop. Workers drain gracefully (30s timeout, then SIGKILL).

Flags:
- `--port 8800` — change dashboard port
- `--no-browser` — don't auto-open
- `--skip-web` — headless, no dashboard (rare)
- `--validate-only` — check config and exit

## How it works

```
┌─────────────────────────────────────────────────────────────┐
│ initial_database_fill.py                                    │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ FastAPI + SSE server (port 8742)                     │   │
│  └──────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ Orchestrator                                         │   │
│  │  - reads lanes.json                                  │   │
│  │  - spawns one run_lane.py per lane                   │   │
│  │  - forwards SIGTERM on Ctrl-C                        │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐     │
│  │ Lane 1   │  │ Lane 2   │  │ Lane 3   │  │ Lane 4   │     │
│  │ local-   │  │ openrtr  │  │ deepseek │  │ opencode │     │
│  │ ollama   │  │ /free    │  │ -direct  │  │ -go      │     │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘     │
│       │             │             │             │            │
│       └─────────────┴──────┬──────┴─────────────┘            │
│                      fill_state.jsonl                       │
│                            (with dispatch lock)             │
└─────────────────────────────────────────────────────────────┘
```

### Lane system

A lane is:
- a provider (`local`, `openrouter`, `deepseek-direct`, `opencode-go`)
- a model
- an article size range (`min_chars`, `max_chars`)
- an API key reference (or `null` for local)
- a fallback chain (other lane names to try on failure)
- priority (higher wins on overlap)

Default 4-lane config (shipped with the script):
| Lane | Provider | Size | Fallback |
|------|----------|------|----------|
| `local-small` | qwen2.5-coder:7b (local) | 0 – 5K | `openrouter-mid` |
| `openrouter-mid` | openrouter/free | 5K – 15K | `deepseek-big` |
| `deepseek-big` | deepseek-v4-flash (DeepSeek direct) | 15K – ∞ | `opencode-big` |
| `opencode-big` | deepseek-v4-flash (OpenCode Go) | 15K – ∞ | `deepseek-big` |

### No overlap

A global dispatch lock serializes the claim. The first lane to acquire
the lock for an article gets it; other lanes see the in-progress row and
skip. Crashed lanes release the claim by the next restart (the dispatch
lock is per-process, not per-claim).

### State file

`~/Library/Application Support/Backpacker Index Manager/fill_state.jsonl`

Append-only JSONL. Schema:
```json
{
  "page_id": 1,
  "slug": "london",
  "size": 18420,
  "lane": "deepseek-big",
  "status": "done",
  "at": "2026-06-06T07:23:15Z",
  "title": "London",
  "input_chars": 18420,
  "output_chars": 4201,
  "elapsed_s": 8.4
}
```

Statuses: `in_progress`, `done`, `failed_attempt`, `failed_permanent`.

The legacy `deepseek_import_state.jsonl` is still read for resume (rows
without a `lane` field are treated as the `default` lane).

## Configuration

### API keys (web UI or files)

`~/Library/Application Support/Backpacker Index Manager/api-keys.json`
(mode 0600):
```json
{
  "openrouter": {"provider": "openrouter", "value": "sk-or-..."},
  "deepseek":   {"provider": "deepseek-direct", "value": "sk-..."},
  "opencode-go": {"provider": "opencode-go", "value": "..."}
}
```

Or add via the dashboard's Configure tab. Values are never displayed in
the API responses — only names and provider.

### Lanes (web UI or files)

`~/Library/Application Support/Backpacker Index Manager/lanes.json`:
```json
{
  "lanes": [
    {
      "name": "local-small",
      "provider": "local",
      "model": "qwen2.5-coder:7b",
      "api_key_ref": null,
      "min_chars": 0,
      "max_chars": 5000,
      "workers": 1,
      "priority": 100,
      "enabled": true,
      "fallback_lanes": ["openrouter-mid"]
    }
  ]
}
```

The web dashboard exposes an editor for this. Save persists to disk.

## Environment overrides

| Var | Purpose |
|-----|---------|
| `FILL_HOST` | bind host (default 127.0.0.1) |
| `FILL_PORT` | dashboard port (default 8742) |
| `FILL_STATE_PATH` | override state file location |
| `FILL_DISPATCH_LOCK` | override dispatch lock file |
| `FILL_SSL_NO_VERIFY=1` | skip cert verification (corporate proxy) |
| `BACKPACKER_SUPPORT_DIR` | override the support directory entirely |
| `OPENROUTER_API_KEY` | OpenRouter key (alternative to api-keys.json) |
| `DEEPSEEK_API_KEY` | DeepSeek key (alt) |
| `OPENCODE_GO_API_KEY` | OpenCode Go key (alt) |
| `LOCAL_PROVIDER_BASE_URL` | override local provider base URL (default `http://localhost:8000/v1`) |
| `OPENROUTER_BASE_URL` | override OpenRouter base URL (default `https://openrouter.ai/api/v1`) |

### Per-lane base URLs

Every lane has a `base_url` field that overrides the provider's built-in
default. Defaults:

- `local` → `http://localhost:8000/v1` (oMLX default; OpenAI-compatible)
- `openrouter` → `https://openrouter.ai/api/v1`
- `deepseek-direct` → `https://api.deepseek.com/chat/completions`
- `opencode-go` → `https://opencode.ai/zen/go/v1`

Edit any lane's base URL in the Configure tab. Useful for self-hosted
OpenCode Zen, a local proxy, or a different upstream for the same provider.

## Files

| File | Purpose |
|------|---------|
| `initial_database_fill.py` | Entry point — starts server + workers |
| `orchestrator.py` | Process supervisor (start, stop, status) |
| `fill_server.py` | FastAPI server with REST + SSE API |
| `fill_state.py` | State file (read, write, query, lock) |
| `lane_config.py` | Lane + key config (load, save, validate) |
| `run_lane.py` | Single-lane worker (subprocess target) |
| `providers/__init__.py` | Provider registry |
| `providers/local.py` | Local provider adapter (OpenAI-compatible, oMLX) |
| `providers/openrouter.py` | OpenRouter adapter |
| `providers/deepseek_direct.py` | DeepSeek direct adapter (reuses existing client) |
| `providers/opencode_go.py` | OpenCode Go adapter (OpenAI + Anthropic modes) |
| `static/dashboard.html` | Single-page UI |
| `static/dashboard.js` | Vanilla JS + SSE wiring |
| `static/style.css` | Dark theme |

## Operational notes

- Re-running after a crash: workers read the state file on start and skip
  any slug with `status=done`. In-flight rows from a crashed worker will
  be retried by the next worker that picks them up (status=`in_progress`
  doesn't count as done).
- The dispatch lock lives at `~/Library/Application Support/Backpacker
  Index Manager/fill_dispatch.lock`. If a worker dies with the lock held,
  the OS releases it.
- A failed lane is a single failure on a single article. The orchestrator
  marks it `failed_attempt` and the article remains available for the
  fallback chain to pick up. If all fallbacks fail, the slug gets
  `failed_permanent` and shows up in the dashboard's "failed" counter.

## What this is NOT

- Not a rewrite of the parsing logic — reuses `deepseek_importer.py`'s
  `build_prompt`, `build_packet`, and the legacy `DeepSeekDirectClient`.
- Not a rewrite of the destination editor — that stays in the Swift app.
- Not a network service — bound to localhost only, no auth.
- Not a rewrite of the public-facing site — separate from this entirely.

## Known gaps (deferred from this build)

- **DB write not yet wired.** The worker currently records `done` in the
  state file but does not call `apply_article` to insert into Postgres.
  Next session: thread the `psql_client` into the worker, run
  `apply_article(data, candidate, run_id)` per success.
- **Swift run-job code not yet removed.** The Swift app's Run/Stop
  buttons and `/agent/jobs/*` endpoints still exist; they will 404 once
  the agent server is restarted. Next session: surgical removal of those
  methods/views/endpoints.
- **Real provider integration not smoke-tested with real keys.** The
  pipeline runs end-to-end with test keys but real-provider success is
  unverified. Run the 6-article pilot from the prior handoff with real
  keys to confirm.
- **`local` health check stubbed.** Worker doesn't pre-flight the
  local URL. If your local server is down, the lane fails every article with a
  network error; it doesn't get disabled.
