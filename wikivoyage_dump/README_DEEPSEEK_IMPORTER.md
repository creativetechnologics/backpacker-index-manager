# DeepSeek Wikivoyage Importer

Program:

```bash
python3 wikivoyage_dump/deepseek_importer.py
```

Running with no arguments opens the interactive TUI menu. No command flags are required for normal use.

Purpose:

- Read Wikivoyage articles from the local dump.
- Convert each article with DeepSeek V4 Flash or an OpenCode-Go-compatible command.
- Resume safely from `wikivoyage_dump/deepseek_import_state.jsonl` and DB `wikivoyage_extraction_runs`.
- Write rows into staging directly by default, so local Docker is not required.
- Optionally write local-only or local+staging if local Docker is fixed later.

## Configure

Use menu option `1. Configure API and database`.

Config is saved outside the repo:

```text
~/.config/backpacker-index/wikivoyage_importer.json
```

Supported config/env values:

- `DEEPSEEK_API_KEY`
- `DEEPSEEK_MODEL`
- `OPENCODE_GO_COMMAND`

Default DeepSeek model is configurable and currently defaults to:

```text
deepseek-v4-flash
```

## Run Examples

Normal use:

```bash
python3 wikivoyage_dump/deepseek_importer.py
```

Then choose:

- `1` configure API/database
- `2` test database connections
- `4` run importer
- `5` classify/skip-list articles before full import

CLI examples still work for automation.

Classification-only prepass, 100 unprocessed random articles:

```bash
python3 wikivoyage_dump/deepseek_importer.py classify --scope pilot --limit 100 --db-target staging --continue-on-error
```

Classification writes:

- `destination_classification` in staging
- local skip list at `wikivoyage_dump/wikivoyage_skip_list.jsonl`

Articles with `parse_strategy` of `route_or_itinerary`, `topic_only`, or `skip` go to the skip list. Full import should focus on `full_destination` and `limited_destination`.

Pilot, 25 random articles, staging DB directly:

```bash
python3 wikivoyage_dump/deepseek_importer.py run --scope pilot --limit 25 --db-target staging
```

Current seeded destinations, staging directly:

```bash
python3 wikivoyage_dump/deepseek_importer.py run --scope existing --db-target staging
```

Tier 1 high-quality Wikivoyage pages:

```bash
python3 wikivoyage_dump/deepseek_importer.py run --scope tier1 --db-target staging --continue-on-error
```

Use OpenCode-Go pathway:

```bash
OPENCODE_GO_COMMAND='opencode-go your-command-here' \
python3 wikivoyage_dump/deepseek_importer.py run --scope pilot --provider opencode-go
```

The OpenCode-Go command must read the prompt from stdin and return the JSON extraction on stdout.

## Scopes

- `existing`: only slugs currently in selected DB target `destinations`.
- `pilot`: random sample, default 25.
- `tier1`: usable/star/guide articles.
- `tier2`: Tier 1 plus longer useful pages.
- `all`: every candidate destination article.
- `failed`: retry pages failed in local state file.

## Database Writes

Default DB target is `staging` because local Docker currently fails on this machine due missing `docker-credential-desktop` helper. The TUI includes a DB connection test and can switch between:

- `staging`: write Flynn staging directly.
- `local`: write local Docker database.
- `both`: write local first, then mirror staging.

For each article, importer writes:

- `destinations`
- `source_documents`
- `wikivoyage_extraction_runs`
- any matching sparse fact tables from extraction JSON

It dynamically maps JSON object keys to actual table columns. Unknown keys are ignored, but raw model output remains in `wikivoyage_extraction_runs.raw_output`.

## Safety

- API keys are not stored in repo.
- Existing destination rows are updated only with Wikivoyage IDs/source/image basics.
- Fact rows use `ON CONFLICT DO NOTHING` or singleton upsert where safe.
- Staging is default target for normal use.
