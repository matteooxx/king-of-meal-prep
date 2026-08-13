#!/usr/bin/env python3
"""Create or update a hardened King of Meal Prep TrueNAS Custom App.

Set KING_ROOT to the application directory and KING_PUBLIC_HOST to the
reverse-proxy hostname before running this script on TrueNAS.
"""
from __future__ import annotations

import json
import os
import sys

APP = "king-of-meal-prep"
PORT = 5002
ROOT = os.environ.get("KING_ROOT", f"/mnt/pool/apps/{APP}").rstrip("/")
PUBLIC_HOST = os.environ.get("KING_PUBLIC_HOST", "mealprep.example.com")
ENV_PATH = f"{ROOT}/runtime/app.env"


def build_compose() -> dict:
    return {
        "services": {
            APP: {
                "container_name": APP,
                "image": f"{APP}:local",
                "restart": "unless-stopped",
                "init": True,
                "user": "1000:1000",
                "read_only": True,
                "ports": [f"127.0.0.1:{PORT}:{PORT}/tcp"],
                "env_file": [ENV_PATH],
                "environment": {
                    "APP_ENV_PATH": "/data/runtime/app.env",
                    "ENV_MARKER_PATH": "/data/runtime/.env-changed",
                    "DB_PATH": "/data/runtime/data.db",
                    "DB_BACKUP_DIR": "/data/runtime/backups",
                    "NUTRITION_DB": "/data/datasets/nutrition.db",
                    "FORCE_HTTPS": "true",
                    "TRUSTED_HOSTS": (
                        f"{PUBLIC_HOST},localhost,127.0.0.1"
                    ),
                },
                "volumes": [
                    f"{ROOT}/runtime:/data/runtime:rw",
                    f"{ROOT}/datasets:/data/datasets:ro",
                ],
                "tmpfs": [
                    "/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777",
                ],
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "mem_limit": "1536m",
                "cpus": "2.0",
                "pids_limit": 128,
                "logging": {
                    "driver": "json-file",
                    "options": {"max-size": "10m", "max-file": "3"},
                },
                "healthcheck": {
                    "test": [
                        "CMD",
                        "python",
                        "-c",
                        (
                            "import urllib.request;"
                            "urllib.request.urlopen("
                            "'http://127.0.0.1:5002/health',timeout=3).read()"
                        ),
                    ],
                    "interval": "30s",
                    "timeout": "5s",
                    "start_period": "30s",
                    "retries": 3,
                },
            }
        }
    }


def main() -> int:
    sys.path.insert(0, "/usr/lib/python3/dist-packages")
    from truenas_api_client import Client  # type: ignore

    if not os.path.isfile(ENV_PATH):
        raise RuntimeError(f"missing runtime environment file: {ENV_PATH}")
    compose = build_compose()

    with Client() as c:
        existing = c.call(
            "app.query", [["name", "=", APP]], {"select": ["id", "name", "state"]}
        )
        if existing:
            print(f"updating {APP} Custom App ...")
            result = c.call(
                "app.update",
                APP,
                {"custom_compose_config": compose},
                job=True,
            )
            print(f"update result: {result}")
        else:
            print(f"creating {APP} Custom App ...")
            result = c.call(
                "app.create",
                {
                    "custom_app": True,
                    "app_name": APP,
                    "custom_compose_config_string": json.dumps(compose),
                },
                job=True,
            )
            print(f"create result: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
