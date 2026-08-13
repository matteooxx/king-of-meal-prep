# Contributing

## Development Setup

Use Python 3.12 and Node.js 22 or newer:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
make check
```

Tests use temporary databases and do not require production data or external
network access.

## Change Guidelines

- Keep runtime state out of source control.
- Preserve the single-writer transaction boundary for cooking, undo, planner
  commit, pantry movement, and prepared movement operations.
- Treat recipe `servings` as batch yield and meal-plan `servings` as portions
  eaten.
- Use canonical pantry units for arithmetic and display units only for UI.
- Validate every API field and reject unknown fields.
- Preserve nutrition source, match basis, and confidence when changing recipe
  or capture persistence.
- Keep portable exports free of secrets and transient authentication/provider
  data; treat full-backup format changes as a compatibility boundary.
- Do not send body profile, pantry contents, meal history, or credentials to an
  external model.
- Add focused tests for schema, state-transition, security, or calculation
  changes.
- Keep first-party browser code dependency-light and accessible by keyboard.

## Before Opening a Pull Request

```bash
make check
docker build -t king-of-meal-prep:review .
git diff --check
```

Explain user-visible behavior, migration impact, rollback requirements, and
the verification performed. Never use live household data in fixtures.
