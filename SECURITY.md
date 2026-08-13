# Security Policy

## Supported Version

Security fixes are applied to the current `2.x` release line. Older snapshots
are unsupported.

## Reporting

Do not open a public issue for a suspected vulnerability. Use the repository
host's private vulnerability-reporting feature, or contact the maintainer
through a private channel listed on the repository profile. Include affected
version, reproduction steps, impact, and any suggested mitigation.

## Deployment Boundary

King of Meal Prep is a single-account private application. It should listen on
loopback, a private network, or behind an authenticated HTTPS reverse proxy. It
is not designed as a public multi-tenant service.

Production deployments should:

- set a random `SECRET_KEY` and bcrypt `ADMIN_PASS_HASH`;
- set `FORCE_HTTPS=true` behind exactly one trusted proxy;
- restrict `TRUSTED_HOSTS` to the real hostname plus local health-check names;
- keep `runtime/app.env`, SQLite files, and backups mode `0600`;
- mount the source and root filesystem read-only where practical;
- drop Linux capabilities and enable `no-new-privileges`;
- back up SQLite before deployment and retain filesystem snapshots;
- update dependencies and rebuild the image regularly.

## Existing Controls

- HttpOnly, Secure-in-HTTPS, SameSite=Strict session cookies
- inactivity expiry and global session revocation epoch
- per-session CSRF token plus requested-with validation
- bcrypt password hashes and hashed, single-use reset tokens
- login, reset, OCR, generation, and barcode-lookup rate limits
- exact trusted-host validation and defensive response headers
- parameterized SQL, foreign keys, explicit state transactions, and ledgers
- SSRF-resistant recipe fetching with DNS/IP validation, connection pinning,
  redirect revalidation, proxy bypass, response limits, and timeouts
- image size/type checks before receipt OCR
- metadata-stripped, dimension- and storage-bounded review images
- bounded, redirect-free, proxy-independent Open Food Facts requests
- portable exports that omit secrets, image bodies, and transient auth/provider
  state, with CSV formula-injection protection
- AES-256-GCM full backups using scrypt-derived keys and authenticated headers
- strict archive, SQLite integrity, foreign-key, and schema validation before
  a backup can be staged for restore
- secret-redacted settings responses and structured log redaction
- a non-root, read-only, capability-free reference container

## Secret Handling

Never commit or attach:

- `runtime/app.env` or any `.env` file;
- databases, WAL/SHM files, backups, nutrition datasets, or exports;
- API keys, SMTP credentials, password hashes, session cookies, or reset links;
- logs or screenshots containing household, pantry, body-profile, or network
  details.

The committed `app.env.example` contains names and safe defaults only.

Portable exports do not include credentials or stored image bodies, but they
still contain recipes, pantry contents, meal history, profile settings, and
other private household data. Treat them as confidential.

A `.kingbackup` contains the database, stored product/receipt photos, and
`app.env`. Encryption does not make a weak or reused passphrase safe. Store the
backup and passphrase separately, never place the passphrase in a command line
or repository, and validate a backup before depending on it. The restore CLI
only stages files in an empty directory; an operator must stop the app and
perform the final replacement deliberately.

## Online Barcode Privacy

Online barcode lookup is enabled by default and can be disabled under
Settings > Scanning. After saved products, the bundled index, and the local
cache miss, the app sends the normalized barcode over HTTPS to
`world.openfoodfacts.org`. For standard GTINs, it may try equivalent
zero-padded representations in successive requests.

Open Food Facts can observe those values, request times, and the server's
public egress IP. The request does not include profile measurements, pantry
contents, recipes, meal history, credentials, cookies, or the administrator
username. Successful responses are cached locally for 90 days and misses for
24 hours.

The review inbox can open an Open Food Facts product page for a valid unknown
GTIN. Following that link is a separate browser request governed by Open Food
Facts' own privacy policy; no photo or household data is uploaded
automatically.
