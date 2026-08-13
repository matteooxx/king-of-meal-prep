# Publishing the Repository

This runbook publishes source code only. It must never publish live household
data, credentials, private network details, or an unsafe earlier Git history.
The commands below target GitLab and work from a clean deployment-NAS
worktree.

## 1. Preconditions

- Work from the intended public `main` branch.
- Confirm the branch begins at the sanitized public root commit.
- Confirm the repository has no private or archive branches.
- Use a clean Git worktree that excludes runtime data and datasets.
- Have a GitLab account and an empty project ready to receive the source.
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

## 6. Create the GitLab Project

In GitLab, create a blank project named `king-of-meal-prep`:

1. Choose the intended personal namespace or group.
2. Set the required visibility.
3. Do not initialize it with a README, license, or `.gitignore`.
4. Keep the project empty until the local checks are complete.

From the NAS source worktree, add the empty project as `origin`:

```bash
cd /path/to/king-of-meal-prep
git status --short --branch
git remote add origin git@gitlab.com:GITLAB_NAMESPACE/king-of-meal-prep.git
git remote -v
git push -u origin main
```

Replace `GITLAB_NAMESPACE` explicitly and inspect the URL before pushing. If
SSH authentication is not configured for the NAS account, use HTTPS:

```bash
git remote set-url origin \
  https://gitlab.com/GITLAB_NAMESPACE/king-of-meal-prep.git
git push -u origin main
```

Enter a personal access token only at Git's password prompt. Never embed a
token in the remote URL, shell history, source tree, or Git configuration.

After the push:

```bash
git ls-remote --heads origin
git status --short --branch
```

The remote should contain only `refs/heads/main`.

## 7. Configure GitLab Protections

In GitLab:

1. Set `main` as the default branch.
2. Protect `main` against force pushes and direct deletion.
3. Require merge requests for changes when collaborators are added.
4. Require the `test`, `javascript`, and `container` pipeline jobs to pass.
5. Enable secret detection, dependency scanning, and protected variables when
   those features are available for the project tier.
6. Disable unused project features and restrict runner access as appropriate.

The committed `.gitlab-ci.yml` runs correctness tests, an authenticated API
smoke test, JavaScript syntax checks, `pip-audit`, and a container build. It
does not publish an image or deploy the application.

## 8. Create the First Release

After the GitLab pipeline succeeds:

```bash
git tag -a v2.2.0 -m "King of Meal Prep 2.2.0"
git push origin v2.2.0
```

Create a GitLab release for that tag in the GitLab UI and use `CHANGELOG.md` as
the release-note source. Use a signed tag when a signing key is already
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
