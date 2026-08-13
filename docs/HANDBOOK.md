# King of Meal Prep Handbook

This is the source-of-truth handbook for developing, operating, backing up,
upgrading, and recovering King of Meal Prep.

## 1. Product Boundary

King of Meal Prep is a single-account household application. It combines:

- profile-based calorie and macro targets;
- weekly meal planning and training-day adjustments;
- recipe import, editing, translation, and generation;
- pantry quantities with normalized units;
- explicit prepared-portion inventory;
- cooking, undo, and meal history;
- future shopping demand;
- barcode and receipt capture;
- durable recognition review and purchase-price history;
- nutrition provenance and confidence;
- portable exports and encrypted full backups.

It is intentionally not multi-user, social, public-facing, or a medical
nutrition system.

## 2. Architecture

```text
Browser / installed PWA
        |
        | HTTPS through a private reverse proxy
        v
Gunicorn (one process, eight gthread threads)
        |
        +-- Flask routes and validation
        +-- domain services and planners
        +-- SQLite application database (read/write)
        +-- SQLite nutrition index (read-only)
        +-- optional Gemini HTTPS API
        +-- optional SMTP server
```

The reference container runs as UID/GID `1000:1000`, drops all capabilities,
uses `no-new-privileges`, and has a read-only root filesystem. Only the runtime
directory and temporary filesystem are writable.

### Source Layout

| Path | Responsibility |
| --- | --- |
| `app.py` | Flask pages, API routes, request validation, headers, health |
| `auth.py` | login, session revocation, password change/reset |
| `db.py` | schema, migrations, settings, connection and transaction layer |
| `meal_service.py` | guarded cook/undo and meal-slot transitions |
| `prepared.py` | prepared batches and reversible movement ledger |
| `planner/solver.py` | proposal generation, scoring, optimistic commit |
| `pantry/dao.py` | pantry stock and reversible consumption |
| `pantry/units.py` | canonical identity and unit conversion |
| `recipes/` | recipe persistence, scrape, safe fetch, LLM, text import |
| `barcodes/` | GTIN validation, Open Food Facts parsing, lookup and cache |
| `recognition.py` | durable unknown-barcode, product-photo, and OCR review |
| `receipts.py` | receipt parsing, reconciliation, pantry commit, price history |
| `image_processing.py` | bounded image decoding and metadata-free JPEG storage |
| `data_portability.py` | portable exports and encrypted backup validation |
| `nutrition/` | nutrition resolution and dataset bootstrap |
| `i18n/` | English-to-Italian static and optional Gemini translation |
| `settings.py` | secret environment persistence and runtime settings |
| `static/`, `templates/` | browser UI and PWA assets |
| `scripts/` | setup, health, nutrition refresh, TrueNAS integration |
| `tests/` | isolated correctness and security regression tests |
| `.gitlab-ci.yml` | GitLab tests, audit, JavaScript checks, image build |

## 3. Persistent Data

Source and state have different lifecycles and must remain separate.

```text
runtime/
  app.env             secrets and process configuration, mode 0600
  app.env.lock        settings write lock
  .env-changed        settings synchronization marker
  data.db             application data, mode 0600
  data.db-wal
  data.db-shm
  backups/            pre-migration backups and short-lived download staging

datasets/
  nutrition.db        replaceable read-only nutrition index
```

Never copy `runtime/` or `datasets/` into a source archive, image layer, Git
commit, CI artifact, issue, or screenshot.

### Application Schema

The SQLite schema version is `7`. Version `7` adds durable per-100 g
nutrition columns (`ean`, `kcal_100g`, `protein_100g`, `carbs_100g`,
`fat_100g`, `fiber_100g`, `nutrition_source`, `nutrition_confidence`,
`nutrition_basis`) to `pantry_items`, and traceable pantry-food logging
columns (`pantry_item_id`, `food_quantity`, `food_unit`, and the same
nutrition provenance trio) to `ad_hoc_meals`. The migration backfills pantry
nutrition from cached barcode profiles and clears unreproducible recipe-level
macro subtotals so incomplete nutrition reads as missing rather than zero.

Profile and configuration:

- `user_profile`
- `preferences`
- `settings_kv`

Recipes and nutrition:

- `recipes`
- `recipe_ingredients`
- `recipe_feedback`
- `translations`
- `user_barcodes`
- `barcode_cache`
- `recognition_inbox`
- `receipt_imports`
- `receipt_items`
- `price_history`
- `llm_calls`

Planning and history:

- `meal_plan`
- `plan_weeks`
- `plan_proposals`
- `ad_hoc_meals`
- `cook_events`

Inventory and compensation:

- `pantry_items`
- `pantry_movements`
- `prepared_batches`
- `prepared_movements`
- `shopping_checks`

Security and maintenance:

- `reset_tokens`
- `migration_history`

SQLite uses WAL mode, foreign keys, a five-second busy timeout, and one
connection per Gunicorn thread. Multi-step state changes use `BEGIN IMMEDIATE`.
Nested transactions are rejected.

## 4. Domain Invariants

These rules are compatibility contracts. Changes require tests and migration
notes.

### Recipe Yield

`recipes.servings` is the yield of one full ingredient list. A recipe yielding
four portions consumes its ingredient list once when four portions are
prepared. Preparing two portions consumes half of that list.

`meal_plan.servings` is the number of portions eaten for that slot. Recipe
macros are per portion and are multiplied by this number in plan/log views.

### Cooking

A cook transition requires an idempotency key. Repeating the same key returns
the existing event without deducting inventory twice.

Fresh mode:

1. Determine portions prepared.
2. Scale raw ingredient use by recipe yield.
3. Consume pantry stock and record movements.
4. Count portions eaten now.
5. Create a prepared batch for the surplus.

Prepared mode:

1. Verify enough unexpired, undiscarded portions exist.
2. Consume oldest-expiring batches first.
3. Record prepared movements.
4. Do not consume raw pantry ingredients.

Undo is compensating, not destructive. It reverses recorded movements while
preserving unrelated later edits. An originating fresh cook cannot be undone
after its stored portions were consumed or manually changed until dependent
uses are undone.

Fresh meals can enter `/recipes/<id>/cook`, which scales ingredient quantities
to the selected batch, presents one step at a time, detects durations for local
timers, and requests the browser Screen Wake Lock when available. Step,
ingredient, timer, and batch state remain browser-local and are cleared after a
successful cook. The final action still uses the same idempotent
`meal_service.patch_slot` transition; guided mode is not a second cook path.

Recipe feedback is one current household opinion per recipe:

- `rating` is nullable or an integer from 1 through 5;
- `preference` is `neutral`, `make_again`, or `avoid`;
- completion prompts are optional and recipe detail remains editable.

Feedback is adherence-neutral: skipping the prompt has no penalty. An explicit
`avoid` is a planner exclusion. Ratings and `make_again` only adjust ranking
among otherwise eligible recipes and never bypass allergies, equipment, time,
rotation, or meal-slot constraints.

### Pantry

Identity is an exact canonical key, never a substring match. Arithmetic uses:

- grams for mass;
- millilitres for volume;
- pieces for count.

The original display unit remains available for the UI. Unknown names receive
a stable name-derived key, so unrelated ingredients never collapse into one
stock item.

Raw pantry items may also have an optional fixed canonical portion size. When
the user splits `400 g` into two portions, the row retains `400 g` as its
authoritative stock and stores a `200 g` planned portion size. Remaining
portions are derived as current stock divided by that size:

- consuming one planned portion subtracts `200 g`;
- a recipe consuming `100 g` leaves `300 g`, displayed as `1.5` portions;
- undoing that recipe restores the quantity and therefore the portion count;
- editing the split recalculates the portion size from the current quantity;
- clearing the split returns the row to quantity-only tracking.

The final consume action uses the smaller of one planned portion and the actual
remainder. Portion metadata does not create duplicate pantry rows or alter
expiry, shopping, or canonical-unit calculations.

### Barcode Lookup

Barcode input accepts 6-32 digits with optional spaces or hyphens. Standard
GTIN-8, UPC-A/GTIN-12, EAN-13, and GTIN-14 values must pass their check digit.
Equivalent zero-padded GTIN forms share one canonical 14-digit identity.

Lookup order is:

1. a user-reviewed `user_barcodes` override;
2. the bundled read-only nutrition index;
3. a fresh local `barcode_cache` entry;
4. Open Food Facts when `barcode_online_lookup` is enabled.

Successful online results are cached for 90 days. Not-found results are cached
for 24 hours to avoid repeated provider calls. An expired positive result may
be used during an Open Food Facts outage, but a definitive provider miss
replaces it. Saving a scanned item records its reviewed name, package quantity,
and unit locally; that override wins on later scans and across equivalent GTIN
representations.

The route is limited to 30 requests per minute. Online responses have a
six-second network timeout and a 512 KiB decoded-body limit; redirects and
environment proxies are disabled.

### Recognition Inbox

`recognition_inbox` is the durable review queue for:

- valid barcodes that miss locally and, when enabled, on Open Food Facts;
- barcode lookups that cannot reach Open Food Facts;
- product photos captured without a barcode;
- uncertain receipt lines.

One open row exists per canonical barcode. Repeated misses update that row and
increment `attempt_count`; they do not create an unbounded duplicate queue.
Resolving a barcode stores the reviewed package name, quantity, and unit in
`user_barcodes`. Resolving a receipt line updates that line before receipt
commit. Dismiss and resolve are terminal states retained for auditability.

Uploaded JPEG, PNG, and WebP files are limited to 8 MiB and 20 million pixels.
They are decoded, orientation-normalized, converted to RGB, resized to at most
1200 pixels on the longest side, re-encoded as a JPEG of at most 1 MiB, and
stored without source metadata. Photos remain private application data. The
Open Food Facts link opens a product page; the app does not upload the photo or
household data.

### Receipt Reconciliation

A receipt upload creates a persistent `receipt_imports` review session and
`receipt_items` candidates. Tesseract runs for at most 30 seconds with English
and Italian language data. Common total, payment, tax, discount, loyalty,
header, and footer lines are removed before candidate creation.

Every candidate must be reviewed as one of:

- `add`: create a new pantry item;
- `merge`: add a compatible amount to the matched active pantry item;
- `skip`: retain the line without changing pantry inventory.

Likely duplicate lines default to `skip`. Commit verifies that the complete,
unchanged set of line IDs was submitted and applies every pantry update and
price record in one `BEGIN IMMEDIATE` transaction. Any conflict rolls back the
whole receipt. Repeating a successful commit is idempotent.

`price_history` stores the reviewed line total and unit price. A receipt detail
shows the most recent earlier price for the same canonical ingredient and only
computes a delta when the units match. The current receipt is excluded from its
own "previous price" comparison.

### Nutrition Provenance

Recipe ingredients, receipt lines, and recognition items store:

- `nutrition_source`: `usda`, `off`, `user`, `manual`, or `unknown`;
- `nutrition_confidence`: `high`, `medium`, `low`, or `unknown`;
- `nutrition_basis`: the exact, prefix, all-words, substring, barcode,
  user-selection, manual-entry, migration, or no-match reason.

Exact USDA matches with convertible amounts are normally high confidence.
Fuzzy USDA and complete Open Food Facts matches are medium; broader,
incomplete, or manually overridden estimates are low. An unconvertible amount
or missing dataset match is unknown. Recipe detail uses the lowest ingredient
confidence as its aggregate and reports how many ingredients are sourced.
Confidence describes traceability and match quality, not medical accuracy.

### Planner

Planning is a two-step optimistic workflow:

1. `proposal` computes without changing the saved plan.
2. `commit` succeeds only if the week version still matches.

Manual, cooked, and locked slots are preserved when configured. Scoring
considers macro fit at the selected serving count, pantry overlap, rotation,
time/equipment constraints, favorites, training targets, prepared portions,
and recipe feedback. The planner maintains a virtual prepared inventory while
filling a week so it cannot allocate one stored portion twice. Each generated
proposal item includes concise reasons such as prepared availability, pantry
fit, target fit, rating, preference, speed, or rotation.

### Shopping

Shopping includes only future planned/substituted meals. It:

1. sums planned portions per recipe;
2. subtracts usable prepared portions;
3. rounds remaining raw demand up to whole recipe batches;
4. converts ingredients to canonical units;
5. subtracts pantry stock;
6. groups the missing amount by aisle.

Optional ingredients are excluded unless the setting enables them. Checked
state is stored by week and canonical item key. The browser queues check
changes while offline and synchronizes them when connectivity returns.

## 5. HTTP Surface

Pages:

- `/today`, `/week`, `/log`
- `/recipes`, `/recipes/<id>`, `/recipes/<id>/cook`
- `/pantry`, `/shopping`, `/scan`
- `/settings`, `/setup`
- `/login`, `/reset-password`

API groups:

- `/api/login`, `/api/logout`, `/api/logout-all`, `/api/me`
- `/api/change-password`, `/api/forgot-password`, `/api/reset-password`
- `/api/settings/*`
- `/api/recipes/*`
- `/api/pantry/*`, `/api/pantry/<id>/consume-portion`,
  `/api/pantry/from-barcode`, `/api/prepared/*`
- `/api/plan/*`, `/api/log/*`
- `/api/shopping/*`
- `/api/scan/receipt`, `/api/receipts/*`
- `/api/recognition-inbox/*`
- `/api/data/export.json`, `/api/data/export.csv.zip`
- `/api/data/backup`, `/api/data/backup/validate`
- `/api/llm/budget`

`GET /health` is unauthenticated and returns readiness for both SQLite
databases plus the schema version. It returns `503` when either database is not
readable.

Every state-changing authenticated API request requires:

```text
X-Requested-With: XMLHttpRequest
X-CSRF-Token: <token returned by /api/me>
```

Unknown fields are rejected. Validation failures use HTTP `422`; stale or
invalid state transitions use `409`.

## 6. Authentication and Secrets

The app has one configured admin username and bcrypt password hash. Flask
session cookies are HttpOnly and SameSite=Strict. HTTPS deployments also set
Secure and HSTS.

Sessions carry a global authentication epoch. Password change, password reset,
or "log out all" advances the epoch and invalidates older cookies.

Password reset tokens:

- are generated with a cryptographic random source;
- are stored only as SHA-256 hashes;
- expire after 30 minutes;
- are single use;
- require an explicitly configured HTTPS `public_base_url`.

Secrets live in `runtime/app.env`. The Settings UI never returns secret values;
it returns only whether a secret is set and its length. Updates are locked,
written atomically, and kept mode `0600`.

## 7. External Data

### Nutrition Index

`nutrition.bootstrap` builds a separate SQLite index atomically. USDA provides
generic foods. Open Food Facts provides branded products and barcodes. The
Open Food Facts loader keeps named European products even when nutrition is
incomplete and indexes equivalent GTIN representations.

USDA-only setup:

```bash
KING_DATASETS=./datasets NUTRITION_DB=./datasets/nutrition.db \
  python -m nutrition.bootstrap --phase usda
```

Full setup:

```bash
KING_DATASETS=./datasets NUTRITION_DB=./datasets/nutrition.db \
  python -m nutrition.bootstrap
```

The full Open Food Facts export is very large. Run refreshes detached,
throttled, and with sufficient free disk. The builder writes
`nutrition.db.build`, validates integrity and minimum row counts, then replaces
the live index atomically.

### Online Barcode Lookup

When `barcode_online_lookup` is enabled and local lookup misses, the normalized
barcode is sent in an HTTPS request to `world.openfoodfacts.org`. For standard
GTINs, equivalent zero-padded representations may be tried in successive
requests. Open Food Facts can observe those values, request times, and the
deployment's public egress IP. No profile, pantry, recipe, meal-history,
credential, or account data is included. The setting is enabled by default and
can be disabled under Settings > Scanning.

An open unknown GTIN also receives a link to its Open Food Facts product page.
Following the link is a browser action and discloses the normal browser request
metadata to Open Food Facts. The app does not automatically submit the stored
photo, reviewed name, pantry state, or receipt.

### Recipe URLs

User-supplied URLs pass through `recipes.safe_fetch`. It permits HTTP/HTTPS
only, rejects URL credentials and non-global addresses, pins the validated IP,
revalidates every redirect, ignores proxy environment variables, limits body
size, and enforces per-hop and total timeouts.

### Gemini

Gemini is optional. It supports generic recipe generation, parsing, and
translation. Code must never send profile measurements, pantry inventory, meal
history, credentials, or training data. Calls are logged by purpose/model/token
count and subject to daily soft caps.

## 8. Development and Tests

Install:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run all local checks:

```bash
make check
```

The check target includes:

- Python unit and migration tests;
- recipe text parser tests;
- SSRF/DNS pinning tests;
- unit and unknown-nutrition tests;
- GTIN normalization, lookup-cache, Open Food Facts parsing, and migration
  tests;
- recognition deduplication, photo metadata removal, receipt rollback and
  idempotence, price history, and nutrition provenance tests;
- portable-export privacy, CSV formula protection, encrypted-backup
  round-trip, wrong-passphrase, tamper, cleanup, and staged-restore tests;
- first-party JavaScript syntax checks;
- an isolated login, CSRF, health, page, settings, capture, export, encrypted
  backup, validation, and prepared API smoke test.

Browser release verification should cover desktop and phone viewports for
Today, Week, Pantry, Recipes, recipe details, Shopping, Log, receipt review,
the recognition inbox, Data & Backup settings, guided cooking, feedback, and
both inventory cooking modes. Check timers, progress resume, Wake Lock
fallback, console errors, horizontal overflow, focus behavior,
fixed-navigation clearance, modal framing, and offline shopping state.

## 9. Docker Operation

Initialize:

```bash
mkdir -p runtime datasets
install -m 600 app.env.example runtime/app.env
docker compose build app
docker compose run --rm --no-deps app \
  python scripts/init-local-env.py --output /data/runtime/app.env --force
docker compose run --rm nutrition
```

Start and inspect:

```bash
docker compose up -d app
docker compose ps
curl -fsS http://127.0.0.1:5002/health
docker compose logs --tail=200 app
```

Stop:

```bash
docker compose down
```

Do not bind the reference Compose port to a public interface without a
separate hardened HTTPS/authentication boundary.

## 10. Production Deployment

Production deployment is intentionally not automated by `make deploy`. The
operator must know the platform's snapshot and rollback mechanisms.

### Preflight

1. Confirm a clean source revision and passing `make check`.
2. Confirm free disk for source, image layers, database backup, and snapshot.
3. Record current image/revision, app health, schema version, and database
   integrity.
4. Notify active users of the maintenance window.

### Safe Sequence

1. Stop the application cleanly.
2. Create a stopped SQLite backup in `runtime/backups/`.
3. Run `PRAGMA quick_check` and `PRAGMA foreign_key_check` against the backup.
4. Create a filesystem/ZFS snapshot of source and runtime.
5. Update the intended public Git worktree or synchronize only tracked source.
   Never replace its `.git/` directory from a repository containing private
   history, and never overwrite `runtime/`, `datasets/`, caches, or local
   environment files.
6. Build the image as a detached, resource-limited background job.
7. Update the app definition without changing runtime/dataset mounts.
8. Start the app and wait for healthy state.
9. Verify `/health`, schema version, login, plan read, pantry read, and one
   non-destructive settings read.
10. Review logs for migration, permission, proxy, or database errors.

Schema migration happens on first startup and creates an additional mode-0600
SQLite backup automatically. The platform snapshot is still required because
it protects source, image configuration, and surrounding state.

### TrueNAS Helper

`scripts/create_app.py` can create or update a hardened Custom App from the
TrueNAS host. Supply site-specific values at execution time:

```bash
KING_ROOT=/mnt/pool/apps/king-of-meal-prep \
KING_PUBLIC_HOST=mealprep.example.com \
  sudo -E python3 scripts/create_app.py
```

Do not commit those values. Site-specific access, pool, reverse-proxy,
snapshot, alerting, and maintenance commands belong in a private device
runbook.

### Post-Deployment Record

Record:

- UTC timestamp and deployed commit/tag;
- backup and snapshot identifiers;
- previous/new schema versions;
- image digest;
- health and smoke results;
- operator and rollback deadline.

## 11. Data Export, Backup, and Restore

### Portable Exports

Settings > Data & Backup provides:

- a structured JSON export;
- a ZIP containing one CSV per portable table and a JSON manifest.

These exports omit `app.env`, reset tokens, barcode-provider cache, LLM call
logs, temporary planner proposals, and stored image bodies. A row records only
whether an image exists. CSV cells beginning with spreadsheet formula
characters are prefixed defensively.

Portable exports still contain private profile, pantry, recipe, planning,
shopping, capture, and meal-history data. They are intended for inspection and
future interoperability; this release does not provide an automatic portable
import.

### Encrypted Full Backups

The full-backup endpoint takes a passphrase of 12-256 characters and creates a
consistent SQLite snapshot plus `app.env` when present. Stored review and
receipt photos are inside the database and therefore included.

The `.kingbackup` format uses:

- AES-256-GCM for encryption and authentication;
- scrypt (`N=32768`, `r=8`, `p=1`) with a random 16-byte salt;
- a random 12-byte nonce;
- authenticated format, creation-time, and schema metadata;
- a deterministic uncompressed tar payload containing `manifest.json`,
  `database.sqlite`, and optionally `app.env`.

Temporary plaintext and partial encrypted files are mode `0600` and removed on
success or failure. The downloaded backup is not retained by the application
after the response closes. The passphrase is neither stored nor recoverable.
Keep it separately from the backup.

Backup validation accepts at most 512 MiB through the web API. It authenticates
and decrypts the archive, rejects unknown/duplicate/non-file/oversized entries,
checks manifest/header consistency, opens SQLite read-only, and requires clean
`quick_check` and `foreign_key_check` results. Validation never changes live
data.

### CLI Validation and Staging

Run from the matching source checkout or container:

```bash
python scripts/restore-backup.py /path/to/backup.kingbackup

mkdir -m 700 /path/to/empty-restore-staging
python scripts/restore-backup.py /path/to/backup.kingbackup \
  --stage-output /path/to/empty-restore-staging
```

The CLI prompts without echo. `KING_BACKUP_PASSPHRASE` is supported for a
controlled automation environment, but must never be written to a repository,
service definition, shell history, or log.

Staging requires an empty directory and writes only:

```text
data.db.restored
app.env.restored     # only when the backup contains app.env
```

Files are installed atomically within the staging directory with mode `0600`.
The command deliberately never replaces live files or starts/stops the app.

### Final Restore Procedure

1. Stop the application.
2. Create a stopped copy and filesystem snapshot of the current failed runtime.
3. Validate the selected `.kingbackup`.
4. Stage it into a new empty mode-`0700` directory.
5. Confirm the reported schema and creation time match the intended source
   revision.
6. Replace `runtime/data.db` and, when intended, `runtime/app.env` from the
   staged files using an atomic, mode-preserving host operation.
7. Remove stale `data.db-wal` and `data.db-shm` only while the app is stopped
   and only after the original runtime was preserved.
8. Start the matching application revision and verify `/health`, login,
   database `quick_check`, foreign keys, pantry, recipes, and one
   non-destructive settings read.

Never start an older application against a newer schema unless that release
explicitly supports it. Restore the matching pre-migration database instead.

Platform recovery should also retain:

- stopped SQLite backups and filesystem snapshots;
- a secret-capable copy of `runtime/app.env`;
- the current source revision, image digest, and app definition.

The nutrition database is reproducible and may be rebuilt, though one recent
copy reduces recovery time.

## 12. Troubleshooting

### Health is degraded

- `database=false`: verify runtime mount, UID/GID, mode, free disk, and SQLite
  integrity.
- `nutrition=false`: verify `datasets/nutrition.db`, read-only mount, schema,
  and completed bootstrap.

### Login fails after deployment

Verify `ADMIN_USER`, bcrypt hash formatting, `SECRET_KEY`, trusted host, proxy
headers, and cookie scheme. Do not print secret values.

### Settings save but disappear after restart

Verify `APP_ENV_PATH`, writable runtime mount, environment marker, mode `0600`,
and any host-side settings synchronization job.

### Cook returns conflict

Reload the slot first. Expected conflicts include stale slot version, reused
idempotency key with different content, insufficient prepared portions, and an
attempt to undo a source batch after its stored portions changed.

### Planner commit returns conflict

The week changed after proposal generation. Generate a new proposal and review
it; do not force the stale payload.

### Shopping checks are pending

The browser retains a local queue while offline. Restore connectivity, reload
Shopping, and confirm queued requests receive successful authenticated
responses.

### Barcode is not recognized

Confirm the digits and check digit, then inspect the result source shown on the
Scan page. Check Settings > Scanning if online lookup is expected. A local miss
is retained in Review, where it can receive a photo, nutrition match, reviewed
name, or dismissal. Later scans use the reviewed override. Rebuild the
nutrition index after importer changes if broader offline coverage is needed.

### Receipt OCR is poor or empty

Retake the image flat, evenly lit, upright, and close enough for text to fill
the frame. Confirm the container includes `tesseract-ocr-eng` and
`tesseract-ocr-ita`. OCR output is only a proposal: correct names, quantities,
units, prices, and add/merge/skip actions before commit. A receipt with no
detected item lines remains available for inspection or discard.

### Backup creation or validation fails

- Confirm the passphrase is 12-256 characters and entered identically.
- Confirm free space in `DB_BACKUP_DIR` for a plaintext SQLite snapshot and
  encrypted output during creation.
- Confirm `app.env` is at most 1 MiB and readable by the app user.
- An incorrect passphrase and a modified/truncated archive intentionally return
  the same authentication failure.
- Validation does not repair a backup. Retain the failed file for diagnosis and
  create a fresh backup from a healthy runtime.

### Restore staging refuses the destination

The destination must be empty by design. Choose a new mode-`0700` directory;
do not point the tool at `runtime/` and do not delete live files to satisfy the
check.

## 13. Release and Publication

Use semantic versions and update `CHANGELOG.md` for user-visible behavior,
schema changes, security fixes, and rollback constraints.

Before any public push, follow [`PUBLIC-GIT.md`](PUBLIC-GIT.md). The checklist
includes publication from a deployment NAS to GitLab. A release is not
publishable if runtime data, private infrastructure details, secrets, or
unsafe Git history remain.

## 14. Known Limits

- One administrator account and one household profile
- No concurrent multi-household isolation
- Nutrition accuracy depends on source data and recipe quantity quality
- Unknown household measures cannot be converted reliably
- Full Open Food Facts refresh is disk and bandwidth intensive
- Online barcode recognition depends on Open Food Facts coverage and
  availability; local reviewed overrides remain authoritative
- Receipt OCR is best-effort and requires human review; it does not infer
  reliable weights, package sizes, taxes, discounts, or merchant-specific
  receipt semantics
- Price comparisons require the same reviewed ingredient identity and unit
- Portable exports have no automatic import path in this release
- Encrypted full backups require a separately retained passphrase and a manual,
  stopped final-restore step
- Offline support is intentionally limited to the shopping workflow and cached
  static shell
- Email reset and Gemini features require external providers
