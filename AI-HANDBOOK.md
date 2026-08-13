# AI Handbook: King of Meal Prep

Read this file before changing the project.

## Status And Scope

King of Meal Prep is a personal, single-household Flask application. This copy
is a technically public-ready snapshot under the MIT license. It contains
source and tests only, never household runtime data.

The application covers meal plans, recipe yields, pantry stock, prepared
portions, nutrition review, shopping, barcode/receipt capture, guided cooking,
feedback, exports, and encrypted backups.

## No-Cloud Runtime

No Amazon service is required. The core stack is Flask, SQLite, local files,
and Docker. Gemini, SMTP, Open Food Facts, and large nutrition imports are
optional. Disable online barcode lookup and leave optional credentials empty
for an entirely local workflow.

```bash
mkdir -p runtime datasets
install -m 600 app.env.example runtime/app.env
docker compose run --rm --no-deps app \
  python scripts/init-local-env.py --output /data/runtime/app.env --force
docker compose up --build -d app
```

The default bind is `127.0.0.1:5002`. On a NAS, persist `runtime/` and
`datasets/`, keep `runtime/app.env` at mode `0600`, and expose the service only
through a trusted LAN or authenticated HTTPS reverse proxy.

### Amazon Replacement Map

| Optional hosted capability | PC/NAS replacement |
| --- | --- |
| Hosted application compute | Flask/Gunicorn in Docker or a local virtual environment |
| Managed database | SQLite in the private `runtime/` directory |
| Object storage | local `runtime/` backups and host-level snapshots |
| Managed AI | leave Gemini unset; core planning remains local |
| Managed email | leave SMTP unset or use an operator-controlled SMTP relay |

## Required Reading

1. `README.md`
2. `docs/HANDBOOK.md`
3. `SECURITY.md`
4. `CHANGELOG.md`
5. `docs/PUBLIC-GIT.md` before a release

## Architecture

- `app.py`: pages, APIs, validation, security headers, health
- `db.py`: SQLite schema, migrations, settings, transactions
- `meal_service.py`: idempotent cooking and undo transitions
- `planner/`: proposal and scoring behavior
- `pantry/`, `prepared.py`: stock and portion ledgers
- `nutrition/`: matching and local index bootstrap
- `barcodes/`, `recognition.py`, `receipts.py`: capture/review paths
- `data_portability.py`: exports and encrypted backup validation
- `tests/`: correctness and security regressions

## Invariants

- Cooking and receipt commits remain transactional and idempotent.
- Undo writes compensating movements and preserves later edits.
- Recipe yield and meal-plan servings remain distinct concepts.
- Pantry identity uses canonical keys and units.
- Missing nutrition remains missing, never silently zero.
- Planner proposals retain optimistic version checks.
- Schema changes include migration, rollback notes, and tests.

## Checks

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/init-local-env.py
make check
docker build -t king-of-meal-prep:local .
```

## Data And Publication Rules

Never commit `runtime/`, `datasets/`, databases, backups, photos, API keys,
SMTP credentials, password hashes, exports, logs, or screenshots containing
household information. Review every migration and generated fixture.

Publish only the sanitized `main` branch. Run tests, `git diff --check`,
gitleaks on both tree and history, and the checks in `docs/PUBLIC-GIT.md`.
Update this file and `docs/HANDBOOK.md` when runtime, schema, backup, or
deployment behavior changes.
