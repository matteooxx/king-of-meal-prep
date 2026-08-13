"""Small strict request-schema helpers used by the Flask routes."""
from __future__ import annotations

import math
from datetime import date
from urllib.parse import urlsplit, urlunsplit


class ValidationError(ValueError):
    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.message = message
        self.field = field

    def as_dict(self) -> dict:
        out = {"error": self.message}
        if self.field:
            out["field"] = self.field
        return out


def object_body(request) -> dict:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValidationError("JSON body must be an object")
    return value


def reject_unknown(body: dict, allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        suffix = "s" if len(unknown) != 1 else ""
        raise ValidationError(f"unknown field{suffix}: {', '.join(unknown)}")


def text(value, field: str, *, required: bool = False, max_length: int = 1000,
         strip: bool = True) -> str | None:
    if value is None:
        if required:
            raise ValidationError("field is required", field=field)
        return None
    if not isinstance(value, str):
        raise ValidationError("must be a string", field=field)
    result = value.strip() if strip else value
    if required and not result:
        raise ValidationError("must not be empty", field=field)
    if len(result) > max_length:
        raise ValidationError(f"must be at most {max_length} characters", field=field)
    return result


def finite_number(value, field: str, *, required: bool = False,
                  minimum: float | None = None,
                  maximum: float | None = None) -> float | None:
    if value is None or value == "":
        if required:
            raise ValidationError("field is required", field=field)
        return None
    if isinstance(value, bool):
        raise ValidationError("must be a number", field=field)
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValidationError("must be a number", field=field)
    if not math.isfinite(result):
        raise ValidationError("must be finite", field=field)
    if minimum is not None and result < minimum:
        raise ValidationError(f"must be at least {minimum:g}", field=field)
    if maximum is not None and result > maximum:
        raise ValidationError(f"must be at most {maximum:g}", field=field)
    return result


def integer(value, field: str, *, required: bool = False,
            minimum: int | None = None, maximum: int | None = None) -> int | None:
    number = finite_number(
        value, field, required=required, minimum=minimum, maximum=maximum
    )
    if number is None:
        return None
    if not number.is_integer():
        raise ValidationError("must be an integer", field=field)
    return int(number)


def enum(value, field: str, choices: set[str], *, required: bool = False) -> str | None:
    result = text(value, field, required=required, max_length=100)
    if result is None:
        return None
    if result not in choices:
        raise ValidationError(
            f"must be one of: {', '.join(sorted(choices))}", field=field
        )
    return result


def iso_date(value, field: str, *, required: bool = False) -> str | None:
    result = text(value, field, required=required, max_length=10)
    if result is None or result == "":
        return result
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError:
        raise ValidationError("must be an ISO date (YYYY-MM-DD)", field=field)


def string_list(value, field: str, *, max_items: int = 100,
                item_length: int = 200) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError("must be a list", field=field)
    if len(value) > max_items:
        raise ValidationError(f"must have at most {max_items} items", field=field)
    return [
        text(item, f"{field}[{index}]", required=True, max_length=item_length)
        for index, item in enumerate(value)
    ]


def http_url(value, field: str, *, required: bool = False,
             https_only: bool = False, max_length: int = 2048) -> str | None:
    result = text(value, field, required=required, max_length=max_length)
    if result is None or result == "":
        return result
    try:
        parsed = urlsplit(result)
    except ValueError:
        raise ValidationError("must be a valid URL", field=field)
    schemes = {"https"} if https_only else {"http", "https"}
    if parsed.scheme.lower() not in schemes or not parsed.hostname:
        allowed = "https" if https_only else "http or https"
        raise ValidationError(f"must use {allowed} with a valid host", field=field)
    if parsed.username or parsed.password:
        raise ValidationError("must not contain credentials", field=field)
    return urlunsplit((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path or "",
        parsed.query or "",
        "",
    ))
