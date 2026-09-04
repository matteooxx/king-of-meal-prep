# King of Meal Prep

King of Meal Prep is a private, self-hosted meal planner that connects weekly
planning, recipe yields, pantry stock, prepared portions, nutrition targets,
and shopping into one workflow.

It is designed for one household account. It is not a medical device and its
nutrition estimates should not be used as medical advice.

## Deployment Boundary

Read this before installing it anywhere.

The application has one administrator account and one household profile. There
is no multi-user isolation and no tenant separation. It stores recipes, pantry
contents, meal history, purchase prices, body measurements, and nutrition
targets, so treat any deployment as private data.

Run it on loopback, on a trusted private network, or behind an authenticated
HTTPS reverse proxy. Do not expose the reference Compose port directly to the
internet. Behind a proxy, set `FORCE_HTTPS=true` and restrict `TRUSTED_HOSTS`
to the real hostname plus local health-check names.

[`SECURITY.md`](SECURITY.md) documents the threat model, the controls already
in place, and the private vulnerability reporting process.

## What It Does

- Builds a seven-day meal plan around nutrition targets, time, equipment,
  allergies, preferences, rotation, food already prepared, and explicit
  recipe feedback, with readable reasons for every proposed pick.
- Provides a resumable guided-cooking view with scaled ingredients,
  one-step-at-a-time instructions, detected timers, and screen-wake support.
- Collects a 1-5 rating plus make-again/avoid intent after cooking and uses it
  to adapt later plans without changing nutrition or safety constraints.
- Tracks recipe batch yield correctly: cooking a four-portion recipe once
  consumes one recipe batch and stores the surplus as explicit inventory.
- Splits raw pantry packages into planned portions, shows the amount in each
  portion, and supports consuming one portion directly from the pantry.
- Maintains reversible pantry and prepared-portion ledgers when a cooked meal
  is undone.
- Imports recipes from a URL, pasted text, or Gemini-assisted generation.
- Reviews every recipe ingredient's amount, unit, food match, nutrient source,
  and counted/not-counted state before saving.
- Creates a future-only shopping list, subtracts pantry and prepared stock,
  and preserves checked items across refreshes and temporary offline use.
- Scans GTIN/UPC/EAN barcodes through saved products, a local nutrition index,
  and an optional Open Food Facts lookup, then remembers reviewed results.
- Retains scanned label nutrition in the pantry and logs an actual gram or
  volume amount into the daily nutrient totals without silently changing stock.
- Keeps unknown barcodes, product photos, and uncertain receipt lines in a
  durable review inbox; valid unknown GTINs can be opened on Open Food Facts.
- Stores receipt scans as review sessions, filters non-item OCR lines, supports
  add/merge/skip reconciliation, and records comparable purchase prices.
- Shows nutrition source, match basis, and confidence on captured items and
  recipe ingredients instead of presenting every estimate as equally certain.
- Exports portable JSON or CSV bundles and creates authenticated,
  passphrase-encrypted full backups with a non-destructive restore tool.
- Provides a responsive dark/light/system interface and installable PWA shell.

## Stack

- Python 3.12, Flask, Gunicorn, and SQLite
- Server-rendered HTML with plain JavaScript and CSS
- Docker or Docker Compose for deployment
- Local USDA/Open Food Facts nutrition index
- Optional Gemini and SMTP integrations

The full architecture, data model, operations, and recovery procedures are in
[`docs/HANDBOOK.md`](docs/HANDBOOK.md).
The comparative product and user-satisfaction audit behind the cooking and
feedback workflow is in [`docs/PRODUCT-RESEARCH.md`](docs/PRODUCT-RESEARCH.md).

## Quick Start With Docker

Requirements: Docker Engine with Compose v2, and disk for the nutrition index.
The `nutrition` job builds a local SQLite index of roughly 230 MB from the USDA
export; allow time and space for it before starting.

```bash
mkdir -p runtime datasets
install -m 600 app.env.example runtime/app.env
docker compose build app
docker compose run --rm --no-deps app \
  python scripts/init-local-env.py --output /data/runtime/app.env --force
docker compose run --rm nutrition
docker compose up -d app
```

Open <http://127.0.0.1:5002>. The first login opens the setup wizard, which
creates the household profile and nutrition targets.

The reference Compose file binds to `127.0.0.1` by default. `KING_BIND_ADDRESS`
can widen that, but only do so once an authenticated HTTPS boundary is in front
of the service.

The default nutrition job downloads USDA data only. Open Food Facts adds broad
barcode coverage but downloads a very large global export; read the nutrition
section of the handbook before enabling it.

Online barcode lookup is enabled by default. When a valid barcode is absent
from saved products and the local index, the app sends that barcode and, for a
standard GTIN, may try equivalent zero-padded forms at
`world.openfoodfacts.org`. Results are cached locally for 90 days and misses
for 24 hours. Disable this behavior under Settings > Scanning for a fully local
lookup path.

Use Settings > Data & Backup for source-independent JSON/CSV exports or a full
encrypted `.kingbackup`. Full backups include the application database,
stored review/receipt photos, and `app.env`; keep the passphrase separately.
Validate or stage a backup without replacing live files:

```bash
python scripts/restore-backup.py backup.kingbackup
python scripts/restore-backup.py backup.kingbackup \
  --stage-output ./restore-staging
```

On Linux, the container runs as UID/GID `1000:1000`. Ensure `runtime/` and
`datasets/` are writable by that identity.

## Local Development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/init-local-env.py
KING_DATASETS=./datasets NUTRITION_DB=./datasets/nutrition.db \
  python -m nutrition.bootstrap --phase usda
```

Use Docker Compose for the application process, or export the values from
`runtime/app.env` with an environment-file-aware process manager before
starting Gunicorn. Do not source that file as shell code.

## Verification

```bash
make check
docker build -t king-of-meal-prep:local .
```

`make check` runs the unit suite, first-party JavaScript syntax checks, and an
isolated authenticated API smoke test.

## Security

The deployment boundary is described above. Never commit `runtime/`,
`datasets/`, a populated `app.env`, databases, backups, or personal
screenshots.

See [`SECURITY.md`](SECURITY.md) for the threat model and reporting process.

## Public Repository

The publication checklist and exact GitHub commands are in
[`docs/PUBLIC-GIT.md`](docs/PUBLIC-GIT.md). It intentionally separates source
from private runtime state and includes history and secret scans before push.
GitHub Actions runs the Python tests, API smoke test, JavaScript syntax checks,
dependency audit, and container build.

## License

The application is available under the [MIT License](LICENSE). Redistributed
fonts and JavaScript bundles retain their own licenses in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
