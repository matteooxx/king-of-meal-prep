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

Host prerequisites: Python 3.12, GNU `make`, and Node.js 22 (the version CI
pins). `make check` is Python tests, a `node --check` pass over first-party
JavaScript, and the isolated API smoke test. Without `make` or `node` the
suite cannot run; `python:3.12-slim` ships neither.

Local tree:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/init-local-env.py
make check
```

Two disposable build paths validate a snapshot. Run both: each sees defects the
other cannot.

### Path A: the project's own image

Validates the Dockerfile. A clean `docker build` proves nothing - the container
must boot and answer. Keep `runtime/` outside the source tree so an audit
cannot leave runtime data in the repository.

```bash
docker build -t king-of-meal-prep:local .

W=$(mktemp -d)
mkdir -p "$W/runtime" "$W/datasets"
chmod 700 "$W/runtime"
sudo chown -R 1000:1000 "$W/runtime" "$W/datasets"

docker run --rm -i -v "$W/runtime":/data/runtime:rw king-of-meal-prep:local \
  python scripts/init-local-env.py --output /data/runtime/app.env --force

docker run -d --name king-audit -p 127.0.0.1:5002:5002 \
  --env-file "$W/runtime/app.env" \
  -e APP_ENV_PATH=/data/runtime/app.env \
  -e ENV_MARKER_PATH=/data/runtime/.env-changed \
  -e DB_PATH=/data/runtime/data.db \
  -e DB_BACKUP_DIR=/data/runtime/backups \
  -e NUTRITION_DB=/data/datasets/nutrition.db \
  -v "$W/runtime":/data/runtime:rw \
  -v "$W/datasets":/data/datasets:ro \
  --tmpfs /tmp:rw,nosuid,nodev,size=128m,mode=1777 \
  king-of-meal-prep:local

docker logs king-audit
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5002/
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5002/login
curl -s http://127.0.0.1:5002/health
docker rm -f king-audit
```

`init-local-env.py` prompts for an admin password of at least 12 characters.
With only `-i` the prompt falls back to stdin and warns that the input may be
echoed; add `-t` when typing it by hand.

Expected: `Listening at: http://0.0.0.0:5002` and
`db init complete: /data/runtime/data.db (schema v7)` in the log; `/` returns
`302` to the login page; `/login` returns `200`; `/health` returns `503` with
`{"checks":{"database":true,"nutrition":false},"schema_version":7,"status":"degraded"}`.

`degraded` with `database=true` is the correct state for a fresh runtime, not a
build defect: `nutrition=false` only means `nutrition.bootstrap` has not built
`datasets/nutrition.db` yet. Booting with no `app.env` is also correct and
exits `3` with `SECRET_KEY must be set in app.env`.

Every `COPY` into the runtime stage carries `--chown=king:king`, and that flag
is load-bearing. The archive stores sources mode `0660` and `COPY` preserves
context permissions, so dropping it yields an image that builds with zero
errors and then dies with `Permission denied` on its own entrypoint under
`USER 1000:1000`. Verify by running the container, never by reading the build
log. The builder-stage `COPY requirements.txt .` and `COPY --from=builder`
need no flag; both run as root before any `USER`.

### Path B: a clean stock runtime

Validates the manifests and the documented checks on a machine that knows
nothing about the project, without using the project's Dockerfile. Mount the
sources read-only and copy them to a writable layer so no check can modify the
tree.

```bash
docker run --rm -v "$PWD":/src:ro -w /work --tmpfs /work:rw,exec,size=2g \
  python:3.12-slim sh -c '
    set -e
    apt-get update -qq
    apt-get install -y --no-install-recommends make nodejs
    cp -r /src/. /work/ && chmod -R u+rwX /work && rm -rf /work/.git
    python -m venv /work/.venv && . /work/.venv/bin/activate
    python -m pip install -q --upgrade pip
    python -m pip install -q -r requirements.txt
    python -m compileall -q .
    make check
  '
```

Expected: `Ran 66 tests`, `OK`, then `API smoke test passed`, exit `0`.
`compileall` is silent.

The two `apt-get` lines are the whole reason this path works, and they belong
inside the throwaway container only. Every requirement installs from a wheel,
so no compiler is needed here; `lxml` is built from source only in the image's
builder stage, which is why that stage installs `build-essential` and
`libxml2-dev`. The test suite and the smoke test need no network, no Gemini
key, and no `datasets/nutrition.db`.

### Why the js-check failure was invisible

`make js-check` used `find ... -exec node --check {} \;`. `find` reports its
own success, not the child's, so the target exited `0` for a file with a
syntax error and equally on a host with no `node` binary at all - printing
nothing either way, because the recipe is `@`-silenced and `node --check` is
quiet on success. The check passed having verified nothing. Path A cannot
expose this, because the image never runs `make`, and CI runs the same target,
so a green CI run proved nothing either. The recipe now pipes through
`xargs -0 -r -n1 node --check`: `123` propagates for a syntax error, `127` for
a missing `node`, and an empty file list still exits `0`. `-exec ... +` is not
a fix - `node --check` inspects only its first argument and silently ignores
the rest, so batching would skip almost every file.

## Data And Publication Rules

Never commit `runtime/`, `datasets/`, databases, backups, photos, API keys,
SMTP credentials, password hashes, exports, logs, or screenshots containing
household information. Review every migration and generated fixture.

Publish only the sanitized `main` branch. Run tests, `git diff --check`,
gitleaks on both tree and history, and the checks in `docs/PUBLIC-GIT.md`.
Update this file and `docs/HANDBOOK.md` when runtime, schema, backup, or
deployment behavior changes.
