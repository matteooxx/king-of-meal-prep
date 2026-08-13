#!/usr/bin/env python3
"""Validate or stage an encrypted King backup without replacing live files."""
from __future__ import annotations

import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data_portability import (  # noqa: E402
    BackupError,
    stage_restore,
    validate_encrypted_backup,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a .kingbackup file or stage restored files in an empty "
            "directory. This command never replaces the live database."
        )
    )
    parser.add_argument("backup", type=Path)
    parser.add_argument(
        "--stage-output",
        type=Path,
        help="empty directory that will receive data.db.restored and app.env.restored",
    )
    args = parser.parse_args()

    passphrase = os.environ.get("KING_BACKUP_PASSPHRASE") or getpass(
        "Backup passphrase: "
    )
    try:
        if args.stage_output:
            report = stage_restore(
                args.backup,
                passphrase,
                args.stage_output,
            )
            action = "staged"
        else:
            report = validate_encrypted_backup(args.backup, passphrase)
            action = "validated"
    except (BackupError, OSError) as exc:
        print(f"restore validation failed: {exc}", file=sys.stderr)
        return 1

    public_report = {
        key: value
        for key, value in report.items()
        if key != "header"
    }
    print(json.dumps({
        "ok": True,
        "action": action,
        **public_report,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
