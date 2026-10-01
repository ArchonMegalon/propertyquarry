#!/usr/bin/env python3
"""Bounded, fully decoded MagicFit contact-sheet validation.

The public eligibility and private acceptance paths share this decoder so an
image signature, truncated stream, or oversized canvas can never stand in for
review evidence.  Pillow is already a pinned PropertyQuarry web dependency;
the import is kept local so non-media callers can import neighboring contract
modules without initializing an image decoder.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import warnings


CONTACT_SHEET_MAX_BYTES = 128 * 1024 * 1024
CONTACT_SHEET_MAX_PIXELS = 40_000_000
CONTACT_SHEET_FORMATS = frozenset({"JPEG", "PNG"})


class MagicFitContactSheetError(ValueError):
    """The supplied bytes are not one bounded, completely decodable image."""


@dataclass(frozen=True)
class MagicFitContactSheet:
    format: str
    width: int
    height: int
    size_bytes: int

    @property
    def pixels(self) -> int:
        return self.width * self.height


def validate_magicfit_contact_sheet_bytes(
    body: bytes,
    *,
    maximum_bytes: int = CONTACT_SHEET_MAX_BYTES,
    maximum_pixels: int = CONTACT_SHEET_MAX_PIXELS,
) -> MagicFitContactSheet:
    """Decode and verify one PNG/JPEG without tolerating partial image data."""

    if not isinstance(body, bytes):
        raise MagicFitContactSheetError("magicfit_contact_sheet_bytes_invalid")
    byte_limit = int(maximum_bytes)
    pixel_limit = int(maximum_pixels)
    if (
        byte_limit <= 0
        or pixel_limit <= 0
        or not body
        or len(body) > byte_limit
    ):
        raise MagicFitContactSheetError("magicfit_contact_sheet_bounds_invalid")

    try:
        from PIL import Image, UnidentifiedImageError
    except (ImportError, ModuleNotFoundError) as exc:
        raise MagicFitContactSheetError(
            "magicfit_contact_sheet_decoder_unavailable"
        ) from exc

    def _metadata() -> tuple[str, int, int]:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(body), formats=tuple(sorted(CONTACT_SHEET_FORMATS))) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
        if (
            image_format not in CONTACT_SHEET_FORMATS
            or isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
            or width * height > pixel_limit
        ):
            raise MagicFitContactSheetError(
                "magicfit_contact_sheet_dimensions_invalid"
            )
        return image_format, width, height

    try:
        image_format, width, height = _metadata()
        # verify() walks the encoded stream and catches broken checksums and
        # truncated containers.  Reopening and load() then forces the actual
        # pixel decoder to consume the complete image.
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(body), formats=(image_format,)) as image:
                image.verify()
            with Image.open(BytesIO(body), formats=(image_format,)) as image:
                if image.size != (width, height):
                    raise MagicFitContactSheetError(
                        "magicfit_contact_sheet_dimensions_changed"
                    )
                image.load()
                if image.size != (width, height):
                    raise MagicFitContactSheetError(
                        "magicfit_contact_sheet_dimensions_changed"
                    )
    except MagicFitContactSheetError:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        ValueError,
        Warning,
    ) as exc:
        raise MagicFitContactSheetError(
            "magicfit_contact_sheet_decode_invalid"
        ) from exc

    return MagicFitContactSheet(
        format=image_format,
        width=width,
        height=height,
        size_bytes=len(body),
    )
