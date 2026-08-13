"""Bounded image decoding and metadata-free review thumbnails."""
from __future__ import annotations

import hashlib
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_REVIEW_BYTES = 1024 * 1024
ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP")


class ImageValidationError(ValueError):
    pass


def decode(raw: bytes, *, max_dimension: int = 4000) -> Image.Image:
    if not raw:
        raise ImageValidationError("image is empty")
    if len(raw) > MAX_INPUT_BYTES:
        raise ImageValidationError("file too large")
    try:
        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
        with Image.open(BytesIO(raw), formats=ALLOWED_FORMATS) as probe:
            if probe.format not in ALLOWED_FORMATS:
                raise UnidentifiedImageError("unsupported format")
            width, height = probe.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("image dimensions exceed limit")
            probe.verify()
        with Image.open(BytesIO(raw), formats=ALLOWED_FORMATS) as source:
            source.load()
            oriented = ImageOps.exif_transpose(source)
            if oriented.mode == "RGBA":
                canvas = Image.new("RGB", oriented.size, "white")
                canvas.paste(oriented, mask=oriented.getchannel("A"))
                image = canvas
            elif oriented.mode != "RGB":
                image = oriented.convert("RGB")
            else:
                image = oriented.copy()
    except (
        UnidentifiedImageError,
        ValueError,
        OSError,
        Image.DecompressionBombError,
    ) as exc:
        raise ImageValidationError(
            "not a valid JPEG, PNG, or WebP image"
        ) from exc
    if max(image.size) > max_dimension:
        image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    return image


def review_jpeg(
    image: Image.Image,
    *,
    max_dimension: int = 1200,
    max_bytes: int = MAX_REVIEW_BYTES,
) -> tuple[bytes, str]:
    thumbnail = image.copy()
    try:
        if max(thumbnail.size) > max_dimension:
            thumbnail.thumbnail(
                (max_dimension, max_dimension),
                Image.Resampling.LANCZOS,
            )
        output = b""
        for quality in (84, 76, 68, 60):
            stream = BytesIO()
            thumbnail.save(
                stream,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
            output = stream.getvalue()
            if len(output) <= max_bytes:
                break
        if len(output) > max_bytes:
            raise ImageValidationError(
                "image remains too large after compression"
            )
    except ImageValidationError:
        raise
    except (OSError, ValueError) as exc:
        raise ImageValidationError("image could not be compressed") from exc
    finally:
        thumbnail.close()
    return output, hashlib.sha256(output).hexdigest()
