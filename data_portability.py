"""Portable exports and authenticated encrypted full backups."""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tarfile
import tempfile
from typing import Iterator
import zipfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

import config
import db


MAGIC = b"KINGBK1\0"
TAG_BYTES = 16
MAX_HEADER_BYTES = 16 * 1024
MAX_ENV_BYTES = 1024 * 1024
CHUNK_BYTES = 1024 * 1024
PORTABLE_VERSION = 1
BACKUP_VERSION = 1

PORTABLE_TABLES = (
    "user_profile",
    "preferences",
    "settings_kv",
    "recipes",
    "recipe_ingredients",
    "recipe_feedback",
    "pantry_items",
    "meal_plan",
    "ad_hoc_meals",
    "user_barcodes",
    "plan_weeks",
    "cook_events",
    "pantry_movements",
    "prepared_batches",
    "prepared_movements",
    "shopping_checks",
    "receipt_imports",
    "receipt_items",
    "price_history",
    "recognition_inbox",
    "translations",
)
PRIVATE_SETTING_KEYS = {
    "auth_epoch",
    "public_base_url",
    "setup_completed_at",
}
BLOB_COLUMNS = {
    ("receipt_imports", "image_jpeg"),
    ("recognition_inbox", "image_jpeg"),
}


class BackupError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _portable_rows(table: str) -> list[dict]:
    if table not in PORTABLE_TABLES:
        raise ValueError("table is not portable")
    connection = db._conn()
    if table == "settings_kv":
        placeholders = ",".join("?" for _ in PRIVATE_SETTING_KEYS)
        rows = connection.execute(
            f"SELECT * FROM settings_kv WHERE key NOT IN ({placeholders}) "
            "ORDER BY key",
            tuple(sorted(PRIVATE_SETTING_KEYS)),
        ).fetchall()
    else:
        rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
    output = []
    for row in rows:
        item = {}
        for key in row.keys():
            value = row[key]
            if (table, key) in BLOB_COLUMNS:
                item["has_image"] = value is not None
                continue
            item[key] = value
        output.append(item)
    return output


def portable_payload() -> dict:
    return {
        "format": "king-of-meal-prep-portable",
        "format_version": PORTABLE_VERSION,
        "schema_version": db.SCHEMA_VERSION,
        "exported_at": _now(),
        "privacy": {
            "contains_secrets": False,
            "contains_images": False,
            "excluded": [
                "app.env",
                "reset tokens",
                "online barcode cache",
                "LLM call logs",
                "temporary planner proposals",
            ],
        },
        "tables": {
            table: _portable_rows(table)
            for table in PORTABLE_TABLES
        },
    }


def portable_json_bytes() -> bytes:
    return (
        json.dumps(
            portable_payload(),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def portable_csv_zip_bytes() -> bytes:
    payload = portable_payload()
    output = BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        manifest = {
            key: value
            for key, value in payload.items()
            if key != "tables"
        }
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        for table, rows in payload["tables"].items():
            columns: list[str] = []
            for row in rows:
                for key in row:
                    if key not in columns:
                        columns.append(key)
            stream = StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=columns or ["empty"])
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    key: _csv_safe(value)
                    for key, value in row.items()
                })
            archive.writestr(f"{table}.csv", stream.getvalue())
    return output.getvalue()


def _csv_safe(value):
    """Prevent exported user text from becoming a spreadsheet formula."""
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


def _derive_key(passphrase: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    if not isinstance(passphrase, str) or len(passphrase) < 12:
        raise BackupError("passphrase must be at least 12 characters")
    if len(passphrase) > 256:
        raise BackupError("passphrase must be at most 256 characters")
    return Scrypt(
        salt=salt,
        length=32,
        n=n,
        r=r,
        p=p,
    ).derive(passphrase.encode("utf-8"))


def _tar_add_bytes(
    archive: tarfile.TarFile,
    name: str,
    value: bytes,
    *,
    mode: int,
) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(value)
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, BytesIO(value))


def _tar_add_file(
    archive: tarfile.TarFile,
    name: str,
    source_path: Path,
    *,
    mode: int,
) -> None:
    info = tarfile.TarInfo(name)
    info.size = source_path.stat().st_size
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    with open(source_path, "rb") as source:
        archive.addfile(info, source)


def _create_plain_backup(path: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="king-backup-build-") as temporary:
        database_path = Path(temporary) / "database.sqlite"
        destination = sqlite3.connect(database_path)
        try:
            db._conn().backup(destination)
        finally:
            destination.close()

        app_env = b""
        env_path = Path(config.APP_ENV_PATH)
        if env_path.exists():
            app_env = env_path.read_bytes()
            if len(app_env) > MAX_ENV_BYTES:
                raise BackupError("app.env exceeds the backup size limit")

        counts = {}
        for table in (
            "recipes",
            "recipe_ingredients",
            "recipe_feedback",
            "pantry_items",
            "meal_plan",
            "prepared_batches",
            "receipt_imports",
            "recognition_inbox",
        ):
            counts[table] = db._conn().execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
        manifest = {
            "format": "king-of-meal-prep-full-backup",
            "format_version": BACKUP_VERSION,
            "schema_version": db.SCHEMA_VERSION,
            "created_at": _now(),
            "contains_app_env": bool(app_env),
            "counts": counts,
        }

        with tarfile.open(path, mode="w") as archive:
            _tar_add_bytes(
                archive,
                "manifest.json",
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    allow_nan=False,
                    indent=2,
                ).encode("utf-8"),
                mode=0o600,
            )
            _tar_add_file(
                archive,
                "database.sqlite",
                database_path,
                mode=0o600,
            )
            if app_env:
                _tar_add_bytes(
                    archive,
                    "app.env",
                    app_env,
                    mode=0o600,
                )
    return manifest


def create_encrypted_backup(output_path: str | Path, passphrase: str) -> dict:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(16)
    nonce = os.urandom(12)
    n, r, p = 1 << 15, 8, 1
    key = _derive_key(passphrase, salt, n=n, r=r, p=p)

    with tempfile.NamedTemporaryFile(
        prefix=".king-backup-plain-",
        suffix=".tar",
        dir=output.parent,
        delete=False,
    ) as temporary:
        plain_path = Path(temporary.name)
    with tempfile.NamedTemporaryFile(
        prefix=".king-backup-encrypted-",
        suffix=".tmp",
        dir=output.parent,
        delete=False,
    ) as temporary:
        encrypted_path = Path(temporary.name)
    try:
        manifest = _create_plain_backup(plain_path)
        header = json.dumps({
            "format_version": BACKUP_VERSION,
            "cipher": "AES-256-GCM",
            "kdf": "scrypt",
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "n": n,
            "r": r,
            "p": p,
            "created_at": manifest["created_at"],
            "schema_version": manifest["schema_version"],
        }, separators=(",", ":"), sort_keys=True).encode("utf-8")
        prefix = MAGIC + struct.pack(">I", len(header)) + header
        encryptor = Cipher(
            algorithms.AES(key),
            modes.GCM(nonce),
        ).encryptor()
        encryptor.authenticate_additional_data(prefix)

        with open(plain_path, "rb") as source, open(encrypted_path, "wb") as target:
            target.write(prefix)
            for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
                target.write(encryptor.update(chunk))
            target.write(encryptor.finalize())
            target.write(encryptor.tag)
        os.chmod(encrypted_path, 0o600)
        os.replace(encrypted_path, output)
        return manifest
    finally:
        for temporary_path in (plain_path, encrypted_path):
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _read_header(source) -> tuple[dict, bytes, int]:
    magic = source.read(len(MAGIC))
    if magic != MAGIC:
        raise BackupError("not a King of Meal Prep encrypted backup")
    length_raw = source.read(4)
    if len(length_raw) != 4:
        raise BackupError("backup header is truncated")
    header_length = struct.unpack(">I", length_raw)[0]
    if header_length <= 0 or header_length > MAX_HEADER_BYTES:
        raise BackupError("backup header is invalid")
    header_raw = source.read(header_length)
    if len(header_raw) != header_length:
        raise BackupError("backup header is truncated")
    try:
        header = json.loads(header_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("backup header is invalid") from exc
    required = {
        "format_version",
        "cipher",
        "kdf",
        "salt",
        "nonce",
        "n",
        "r",
        "p",
        "created_at",
        "schema_version",
    }
    if not isinstance(header, dict) or not required.issubset(header):
        raise BackupError("backup header is incomplete")
    if (
        header["format_version"] != BACKUP_VERSION
        or header["cipher"] != "AES-256-GCM"
        or header["kdf"] != "scrypt"
    ):
        raise BackupError("backup format is not supported")
    prefix = magic + length_raw + header_raw
    return header, prefix, len(prefix)


def _decrypt_to_tar(
    input_path: Path,
    passphrase: str,
    output_path: Path,
) -> dict:
    size = input_path.stat().st_size
    with open(input_path, "rb") as source:
        header, prefix, ciphertext_offset = _read_header(source)
        ciphertext_bytes = size - ciphertext_offset - TAG_BYTES
        if ciphertext_bytes <= 0:
            raise BackupError("backup payload is truncated")
        try:
            salt = base64.b64decode(header["salt"], validate=True)
            nonce = base64.b64decode(header["nonce"], validate=True)
            n = int(header["n"])
            r = int(header["r"])
            p = int(header["p"])
        except (binascii.Error, ValueError, TypeError) as exc:
            raise BackupError("backup key parameters are invalid") from exc
        if len(salt) != 16 or len(nonce) != 12 or (n, r, p) != (1 << 15, 8, 1):
            raise BackupError("backup key parameters are unsupported")
        key = _derive_key(passphrase, salt, n=n, r=r, p=p)

        source.seek(size - TAG_BYTES)
        tag = source.read(TAG_BYTES)
        source.seek(ciphertext_offset)
        decryptor = Cipher(
            algorithms.AES(key),
            modes.GCM(nonce, tag),
        ).decryptor()
        decryptor.authenticate_additional_data(prefix)
        remaining = ciphertext_bytes
        try:
            with open(output_path, "wb") as target:
                while remaining:
                    chunk = source.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        raise BackupError("backup payload is truncated")
                    remaining -= len(chunk)
                    target.write(decryptor.update(chunk))
                target.write(decryptor.finalize())
        except InvalidTag as exc:
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass
            raise BackupError(
                "incorrect passphrase or corrupted backup"
            ) from exc
    return header


def _extract_validated_tar(tar_path: Path, output_dir: Path) -> dict:
    allowed = {
        "manifest.json": 1024 * 1024,
        "database.sqlite": 4 * 1024 * 1024 * 1024,
        "app.env": MAX_ENV_BYTES,
    }
    found: set[str] = set()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, mode="r:") as archive:
        for member in archive.getmembers():
            if (
                member.name not in allowed
                or not member.isfile()
                or member.size < 0
                or member.size > allowed[member.name]
                or member.name in found
            ):
                raise BackupError("backup archive contains an invalid entry")
            found.add(member.name)
            source = archive.extractfile(member)
            if source is None:
                raise BackupError("backup archive is unreadable")
            destination = output_dir / member.name
            with open(destination, "wb") as target:
                shutil.copyfileobj(source, target, CHUNK_BYTES)
            os.chmod(destination, 0o600)
    if not {"manifest.json", "database.sqlite"}.issubset(found):
        raise BackupError("backup archive is incomplete")

    try:
        manifest = json.loads((output_dir / "manifest.json").read_text("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("backup manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != "king-of-meal-prep-full-backup"
        or manifest.get("format_version") != BACKUP_VERSION
    ):
        raise BackupError("backup manifest is not supported")
    if (
        not isinstance(manifest.get("schema_version"), int)
        or manifest["schema_version"] < 1
        or not isinstance(manifest.get("contains_app_env"), bool)
    ):
        raise BackupError("backup manifest is incomplete")

    database_path = output_dir / "database.sqlite"
    connection = sqlite3.connect(
        f"file:{database_path}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        schema_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()
    if quick_check != "ok" or foreign_keys:
        raise BackupError("backup database failed integrity validation")
    required_tables = {"recipes", "pantry_items", "settings_kv"}
    if not required_tables.issubset(tables):
        raise BackupError("backup database is missing required tables")
    if manifest["schema_version"] != schema_version:
        raise BackupError("backup schema metadata does not match its database")
    if manifest["contains_app_env"] != ("app.env" in found):
        raise BackupError("backup environment metadata is inconsistent")
    return {
        "manifest": manifest,
        "schema_version": schema_version,
        "quick_check": quick_check,
        "foreign_key_errors": 0,
        "contains_app_env": "app.env" in found,
        "database_bytes": database_path.stat().st_size,
    }


@contextmanager
def opened_backup(
    input_path: str | Path,
    passphrase: str,
) -> Iterator[tuple[dict, Path]]:
    with tempfile.TemporaryDirectory(prefix="king-backup-open-") as temporary:
        root = Path(temporary)
        tar_path = root / "backup.tar"
        extract_dir = root / "contents"
        header = _decrypt_to_tar(Path(input_path), passphrase, tar_path)
        report = _extract_validated_tar(tar_path, extract_dir)
        if header.get("schema_version") != report["schema_version"]:
            raise BackupError("backup header metadata is inconsistent")
        if header.get("created_at") != report["manifest"].get("created_at"):
            raise BackupError("backup creation metadata is inconsistent")
        report["header"] = header
        yield report, extract_dir


def validate_encrypted_backup(
    input_path: str | Path,
    passphrase: str,
) -> dict:
    with opened_backup(input_path, passphrase) as (report, _):
        return report


def stage_restore(
    input_path: str | Path,
    passphrase: str,
    output_dir: str | Path,
) -> dict:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(destination.iterdir()):
        raise BackupError("restore output directory must be empty")
    with opened_backup(input_path, passphrase) as (report, contents):
        staged: list[tuple[Path, Path]] = []
        database_target = destination / "data.db.restored"
        database_temporary = destination / ".data.db.restored.tmp"
        staged.append((database_temporary, database_target))
        if (contents / "app.env").exists():
            staged.append((
                destination / ".app.env.restored.tmp",
                destination / "app.env.restored",
            ))
        try:
            shutil.copy2(contents / "database.sqlite", database_temporary)
            os.chmod(database_temporary, 0o600)
            if (contents / "app.env").exists():
                env_temporary = destination / ".app.env.restored.tmp"
                shutil.copy2(contents / "app.env", env_temporary)
                os.chmod(env_temporary, 0o600)
            for temporary_path, target_path in staged:
                os.replace(temporary_path, target_path)
        except Exception:
            for temporary_path, target_path in staged:
                for path in (temporary_path, target_path):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            raise
    return report
