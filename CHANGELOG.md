# Changelog

All notable changes are documented here. The project follows semantic
versioning from the first public release.

## Unreleased

### Changed

- Dependency updates: `beautifulsoup4` 4.15.0, `httpx` 0.28.1 and
  `recipe-scrapers` 15.12.0.
- Gunicorn advances to 26.x. The container still runs a single gthread worker
  with eight threads and a 120-second timeout.

## 2.3.0 - 2026-09-03

### Added

- A structured recipe-nutrition review with per-ingredient amount, unit,
  dataset match, calculation status, source, and macro preview.
- Pantry-first daily food logging that calculates label nutrition from an
  entered amount while leaving stock unchanged.
- Optional raw-pantry portion splitting in manual and barcode capture flows,
  with live per-portion amounts and a guarded one-portion consume action.
- A full-screen guided-cooking mode with scaled batch ingredients, persistent
  ingredient and step progress, inline ingredient reminders, local timers,
  keyboard/swipe navigation, and Wake Lock support.
- Recipe ratings and explicit make-again/avoid feedback on recipe detail and
  after meal completion.
- Human-readable reasons for every generated meal in planner proposal review.
- A dated comparative product and App Store satisfaction audit documenting
  which interaction patterns were adopted and why.

### Changed

- Recipe URL, text, and generated imports retain structured ingredient
  identity and provenance instead of flattening back to lossy text before
  save.
- Scanned and remembered barcode products retain available per-100-gram
  nutrition on pantry rows; cached profiles are recovered during migration.
- Count-based packaged foods ask for a weighed amount instead of borrowing a
  whole-fruit or vegetable weight from a word inside the product name.
- Incomplete nutrition is shown as missing rather than zero and requires an
  explicit confirmation before recipe save.
- Planned portion size remains stable while recipes consume or restore raw
  stock, so the pantry can show fractional portions after partial use without
  changing the authoritative total quantity.
- Automatic planning excludes recipes marked avoid and modestly favors strong
  ratings and make-again intent while preserving all existing nutrition,
  allergy, equipment, time, rotation, pantry, and prepared-food constraints.
- Fresh meals open guided cooking from Today and Week; prepared portions keep
  the faster direct logging path.
- The publication runbook and the required status checks now target GitHub
  Actions; the GitLab CI definition has been removed.

### Security

- `cryptography` is pinned to `50.0.1`, resolving PYSEC-2026-3552. The previous
  `49.0.0` pin was reported by `pip-audit` in continuous integration.
- The workflow uses `actions/checkout`, `actions/setup-python` and
  `actions/setup-node` v7, which run on a supported Node runtime.
- A `.gitattributes` file normalises line endings to LF so that a checkout on
  Windows cannot introduce CRLF into scripts executed on Linux.

### Migration

- SQLite schema version advances to `7`. Version `5` adds optional raw-pantry
  portion sizes; version `6` adds per-recipe rating and planning preference;
  version `7` adds durable pantry nutrition and traceable pantry-food log
  fields. Existing cached barcode profiles are backfilled where possible.

## 2.2.0 - 2026-07-18

### Added

- A durable recognition inbox for unknown barcodes, product photos, and
  uncertain receipt lines, with photo attachment, nutrition suggestions,
  resolve/dismiss actions, and Open Food Facts contribution links.
- Persistent receipt review sessions with bounded image processing, OCR line
  filtering, duplicate warnings, pantry add/merge/skip decisions, atomic
  commit, discard, and idempotent replay.
- Purchase price history with previous-price comparisons that exclude the
  receipt currently being viewed.
- Nutrition provenance on recipe and receipt ingredients, including source,
  match basis, high/medium/low/unknown confidence, and recipe-level summaries.
- Portable JSON and CSV ZIP exports that omit secrets, image bodies, and
  transient operational tables.
- AES-256-GCM full backups derived from a passphrase with scrypt, authenticated
  metadata, integrity validation, and a non-destructive staged restore CLI.

### Changed

- Unknown valid barcode misses and Open Food Facts outages now create or update
  one durable review item instead of ending as an ephemeral scan error.
- Receipt OCR now supports English and Italian and rejects common totals,
  payment, tax, loyalty, header, and footer lines before review.
- Stored product and receipt images are normalized to bounded,
  metadata-free JPEGs.
- Upload limits are route-specific: normal image routes remain bounded to
  8 MiB while backup validation accepts encrypted archives up to 512 MiB.
- Added native GitLab CI for tests, API smoke, JavaScript syntax checks,
  dependency auditing, and container builds.
- Reworked the public-release checklist for a clean NAS-hosted worktree and
  GitLab publication.

### Security And Privacy

- Full backups are encrypted and authenticated but contain secrets and private
  household data after decryption; portable exports exclude secrets but still
  contain personal application data.
- Spreadsheet formula prefixes are neutralized in CSV exports.
- Backup archives, database integrity, schema metadata, paths, entry types,
  sizes, and environment-file consistency are validated before staging.
- Product photos are decoded with format, byte, pixel, and dimension limits;
  EXIF and other source metadata are not retained.

### Migration

- SQLite schema version advances to `4`.
- Existing recipe ingredients gain nutrition source, confidence, and basis
  fields; known dataset keys are conservatively marked as migrated with low
  confidence.
- New tables store recognition items, receipt imports and lines, and price
  history.

## 2.1.0 - 2026-07-18

### Added

- Optional Open Food Facts lookup for barcodes absent from saved products and
  the bundled nutrition index.
- Local positive and negative lookup caching with package quantity, brand, and
  available nutrition data.
- A Settings > Scanning control for disabling outbound barcode lookups.
- GTIN-8, UPC-A, EAN-13, and GTIN-14 normalization, check-digit validation,
  and equivalent-code matching.

### Changed

- Reviewed barcode names now retain their package quantity and unit for later
  scans.
- The Open Food Facts importer covers common European origins, retains named
  products with incomplete nutrition, and indexes equivalent GTIN forms.
- Scan results identify whether data came from a reviewed override, offline
  index, local cache, or online lookup.

### Security And Privacy

- Barcode lookup is limited to 30 requests per minute.
- Open Food Facts responses are time- and size-bounded; redirects and
  environment proxies are disabled.
- Documentation now discloses that online misses send the normalized barcode
  to Open Food Facts and expose the server's public egress IP.

### Migration

- SQLite schema version advances to `3`.
- Existing `user_barcodes` rows are retained with a default quantity of one.
- A new `barcode_cache` table stores online results and expiry timestamps.

## 2.0.0 - 2026-07-18

### Added

- Explicit prepared-batch inventory with fridge/freezer expiry and editing.
- Fresh-batch and prepared-portion cooking modes across all meal views.
- Reversible prepared-portion movements tied to cook events.
- Persistent shopping checks with an offline synchronization queue.
- Raw-text recipe import and recipe-title translation.
- PWA manifest, service worker, theme selection, desktop sidebar, and mobile
  bottom navigation.
- Public repository handbook, CI, Compose setup, smoke test, and license
  provenance.

### Changed

- Recipe ingredient use now scales by batch yield instead of multiplying the
  full ingredient list by every eaten portion.
- Planner scoring accounts for serving count and prioritizes prepared food.
- Shopping demand rounds up to whole recipe batches and subtracts available
  prepared portions.
- Pantry matching uses exact canonical keys rather than name substrings.
- Unknown or unconvertible quantities no longer claim arbitrary per-100-gram
  nutrition.
- Difficulty labels and prepared shelf-life defaults are editable.
- The interface was redesigned for denser daily operation on desktop and
  mobile.

### Migration

- SQLite schema version advances to `2`.
- Startup creates a mode-`0600` SQLite backup before applying migrations.
- Existing application data is retained; prepared inventory begins empty.
