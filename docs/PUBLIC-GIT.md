# Publishing the Repository

This runbook publishes source code only. It must never publish live household
data, credentials, private network details, or an unsafe earlier Git history.
The commands below target GitHub and work from a clean deployment-NAS
worktree.

## 1. Preconditions

- Work from the intended public `main` branch.
- Confirm the branch begins at the sanitized public root commit.
- Confirm the repository has no private or archive branches.
- Use a clean Git worktree that excludes runtime data and datasets.
- Have a GitHub account and an empty repository ready to receive the source.
- Use `gitleaks` for the final scan when it is available.

Do not install development packages into a NAS appliance merely to run this
checklist. Run the full test and scan suite on a development machine or in CI;
the NAS only needs Git to inspect and push the already-reviewed repository.

## 2. Test the Release

On a development machine with Python 3.12, Node.js, and Docker:

```bash
make check
docker build -t king-of-meal-prep:release .
git diff --check
git status --short
```

The status should contain only reviewed release files before the final commit,
then be empty afterward.

## 3. Inspect Tracked Paths

```bash
git ls-files | sort
git ls-files | grep -Ei \
  '(^|/)(runtime|datasets|backups|\.scratch|\.env|.*\.db|.*\.sqlite|.*\.kingbackup)'
git ls-files -ci --exclude-standard
```

The second and third commands should return no output. `app.env.example` is the
only allowed environment template.

## 4. Scan Content and History

Search for secret shapes and site-specific metadata:

```bash
git grep -nEI \
  '(password|passwd|secret|token|api.?key|private.?key|BEGIN [A-Z ]*PRIVATE KEY)' \
  -- ':!SECURITY.md' ':!docs/PUBLIC-GIT.md' ':!app.env.example'

git grep -nEI \
  '(tailscale|truenas|/mnt/[^ ]+|10\.[0-9]+\.[0-9]+\.[0-9]+|192\.168\.|100\.(6[4-9]|[7-9][0-9])\.)'

git log main -p --all-match -G \
  '(ADMIN_PASS_HASH=.+|GEMINI_API_KEY=.+|SMTP_PASS=.+|SECRET_KEY=.+)'
```

Review every match; variable names, documentation, test-only values, and
generic private-network defenses can be legitimate. Populated values are not.

Run a dedicated scanner when available:

```bash
gitleaks git . --log-opts=main
```

Before pushing from the NAS, inspect every available ref and commit:

```bash
git status --short --branch
git for-each-ref --format='%(refname) %(objectname:short)'
git log --oneline --decorate --all
git remote -v
```

Only the reviewed public `main` history should exist. Do not use
`git push --all`, even from a repository that currently appears clean.

## 5. Review Privacy Manually

Check for:

- names, personal email addresses, usernames, home domains, and tailnet names;
- public or private IP addresses tied to the deployment;
- NAS pool, dataset, host, account, or cloud identifiers;
- screenshots containing recipes, pantry stock, body metrics, or browser data;
- logs, fixtures, database dumps, backups, portable exports, cookies, and
  reset links.

Generic examples such as `mealprep.example.com` and `/mnt/pool/apps/...` are
acceptable.

## 6. Create the GitHub Repository

In GitHub, create a blank repository named `king-of-meal-prep`:

1. Choose the intended personal account or organization.
2. Set the required visibility. Publish private first, then make the
   repository public only after the pipeline has passed and the secret scans
   in section 4 are complete.
3. Do not initialize it with a README, license, or `.gitignore`.
4. Keep the repository empty until the local checks are complete.

Add the empty repository under a dedicated remote name. Do not reuse or
overwrite `origin`: a deployment worktree may already carry a private remote,
and a dedicated name keeps the public destination unambiguous.

```bash
cd /path/to/king-of-meal-prep
git status --short --branch
git remote add github git@github.com:GITHUB_NAMESPACE/king-of-meal-prep.git
git remote -v
git push -u github HEAD:refs/heads/main
```

Replace `GITHUB_NAMESPACE` explicitly and inspect the URL before pushing.
Pushing `HEAD:refs/heads/main` publishes exactly one branch; never use
`git push --all`, which would also publish any local or tooling refs.

If SSH authentication is not configured for the account performing the push,
use HTTPS:

```bash
git remote set-url github \
  https://github.com/GITHUB_NAMESPACE/king-of-meal-prep.git
git push -u github HEAD:refs/heads/main
```

Enter a personal access token only at Git's password prompt or through a
credential helper. Never embed a token in the remote URL, shell history,
source tree, or Git configuration.

After the push:

```bash
git ls-remote --heads github
git status --short --branch
```

The remote should contain only `refs/heads/main`.

## 7. Configure GitHub Protections

In the repository settings:

1. Confirm `main` is the default branch.
2. Add a branch protection rule for `main` that blocks force pushes and
   deletion.
3. Require pull requests for changes when collaborators are added.
4. Require the `test` and `container` status checks to pass before merging.
5. Enable secret scanning with push protection, Dependabot alerts, and
   Dependabot security updates. The committed `.github/dependabot.yml` already
   declares the update schedule.
6. Enable private vulnerability reporting so `SECURITY.md` has a working
   private channel.
7. Disable unused repository features and restrict Actions permissions to the
   read-only default.

The committed `.github/workflows/ci.yml` runs correctness tests, an
authenticated API smoke test, JavaScript syntax checks, `pip-audit`, and a
container build. It does not publish an image or deploy the application.

## 8. Create the First Release

After the GitHub Actions workflow succeeds:

```bash
git tag -a v2.3.0 -m "King of Meal Prep 2.3.0"
git push github v2.3.0
```

Tag the version that `CHANGELOG.md` actually documents as the newest release;
do not reuse a version that already has a dated entry. Create a GitHub release
for that tag and use `CHANGELOG.md` as the release-note source. Use a signed tag when a signing key is already
configured; do not create or copy private signing keys merely for this step.

Do not attach runtime directories, databases, datasets, logs, source archives,
or private screenshots to the release.

## 9. Ongoing Publication Rules

- Run tests and secret/history scans before every release.
- Keep deployment-specific procedures in a private operator handbook.
- Pull with `--ff-only` and inspect changes before rebuilding production.
- Rotate a credential immediately if it ever enters Git; deleting the line is
  not sufficient because history, forks, caches, and clones may retain it.
- Use a reviewed history-rewrite tool only after rotation, then coordinate a
  force push with every contributor.
- Publish migrations and rollback constraints in `CHANGELOG.md`.
