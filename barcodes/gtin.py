"""GTIN validation and equivalent UPC/EAN representations."""
from __future__ import annotations

from dataclasses import dataclass
import re


GTIN_LENGTHS = (8, 12, 13, 14)


class BarcodeError(ValueError):
    """The supplied barcode cannot be normalized safely."""


@dataclass(frozen=True)
class Barcode:
    raw: str
    canonical: str
    aliases: tuple[str, ...]
    is_gtin: bool


def valid_check_digit(code: str) -> bool:
    if not code.isdigit() or len(code) not in GTIN_LENGTHS:
        return False
    body = code[:-1]
    total = 0
    for position, digit in enumerate(reversed(body), start=1):
        total += int(digit) * (3 if position % 2 else 1)
    return (-total) % 10 == int(code[-1])


def parse(value: str) -> Barcode:
    raw = (value or "").strip()
    if not raw:
        raise BarcodeError("barcode is required")
    if re.search(r"[^\d\s-]", raw):
        raise BarcodeError("barcode may contain only digits, spaces, and hyphens")
    digits = re.sub(r"[\s-]+", "", raw)
    if len(digits) < 6 or len(digits) > 32:
        raise BarcodeError("barcode must contain 6-32 digits")

    if len(digits) not in GTIN_LENGTHS:
        return Barcode(
            raw=digits,
            canonical=digits,
            aliases=(digits,),
            is_gtin=False,
        )
    if not valid_check_digit(digits):
        raise BarcodeError("barcode check digit is invalid")

    canonical = digits.zfill(14)
    aliases: list[str] = [digits]
    for width in GTIN_LENGTHS:
        prefix = canonical[:-width]
        candidate = canonical[-width:]
        if prefix.strip("0") or not valid_check_digit(candidate):
            continue
        if candidate not in aliases:
            aliases.append(candidate)
    return Barcode(
        raw=digits,
        canonical=canonical,
        aliases=tuple(aliases),
        is_gtin=True,
    )
