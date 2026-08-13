.PHONY: build check test js-check smoke up down logs deploy

IMAGE := king-of-meal-prep:local
PYTHON ?= python3

build:
	docker build -t $(IMAGE) .

test:
	$(PYTHON) -m unittest discover -s tests -v

js-check:
	@find static/js -type f -name '*.js' ! -path '*/vendor/*' -exec node --check {} \;

smoke:
	$(PYTHON) scripts/smoke-api.py

check: test js-check smoke

up:
	docker compose up -d app

down:
	docker compose down

logs:
	docker compose logs -f --tail=200 app

# Production deployment includes a stopped SQLite backup, a ZFS snapshot,
# detached build throttling, and post-deploy verification. Keep it runbook
# driven so a source sync can never overwrite runtime data.
deploy:
	@echo "Deployment is intentionally disabled here; follow docs/HANDBOOK.md."
	@false
