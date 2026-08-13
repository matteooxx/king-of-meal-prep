#!/usr/bin/env python3
"""Create a mode-0600 local runtime environment file."""
from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
from pathlib import Path

import bcrypt


def _password() -> str:
    first = getpass.getpass("Admin password: ")
    if len(first) < 12:
        raise SystemExit("password must contain at least 12 characters")
    if first != getpass.getpass("Confirm password: "):
        raise SystemExit("passwords do not match")
    return first


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("runtime/app.env"),
        help="destination environment file",
    )
    parser.add_argument("--username", default="admin")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing environment file",
    )
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.username):
        raise SystemExit("username contains unsupported characters")
    if args.output.exists() and not args.force:
        raise SystemExit(f"{args.output} already exists; refusing to overwrite it")

    password_hash = bcrypt.hashpw(
        _password().encode("utf-8"), bcrypt.gensalt()
    ).decode("ascii")
    values = {
        "ADMIN_USER": args.username,
        "ADMIN_PASS_HASH": password_hash,
        "SECRET_KEY": secrets.token_urlsafe(48),
        "SESSION_INACTIVITY_HOURS": "24",
        "FORCE_HTTPS": "false",
        "TRUSTED_HOSTS": "localhost,127.0.0.1",
        "GEMINI_API_KEY": "",
        "SMTP_HOST": "smtp.gmail.com",
        "SMTP_PORT": "587",
        "SMTP_USER": "",
        "SMTP_PASS": "",
        "SMTP_FROM": "",
        "OWNER_EMAIL": "",
        "DB_PATH": "./runtime/data.db",
        "NUTRITION_DB": "./datasets/nutrition.db",
        "APP_ENV_PATH": "./runtime/app.env",
        "ENV_MARKER_PATH": "./runtime/.env-changed",
        "DB_BACKUP_DIR": "./runtime/backups",
    }

    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, args.output)
    print(f"created {args.output} with mode 0600")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
