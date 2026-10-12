"""Pillow decoding for thumbnails, previews and poster frames (M4).

Only a job's rlimited tool child imports and runs this. Pillow may open only the formats in
:data:`ALLOWED_FORMATS` (so a misnamed file never reaches another decoder), its
decompression-bomb check is an error (an image above :data:`MAX_IMAGE_PIXELS` is
"unsupported"), and truncated files fail. HEIC and HEIF decode through pillow-heif, which
applies the container's rotation and mirroring itself; every other format is turned upright
with ``ImageOps.exif_transpose``.

A rendering is: ``draft()`` for JPEG (decode at a reduced scale, still at least twice the
target), the EXIF orientation, a fit-within resize that never enlarges, a conversion to sRGB
when the image carries an ICC profile, and a WebP save that writes no EXIF, XMP or ICC data.
Animated images use their first frame.
"""

from __future__ import annotations

import io
import os
import warnings
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Final

import pillow_heif
from PIL import AvifImagePlugin, Image, ImageCms, ImageFile, ImageOps, UnidentifiedImageError

from ..protocol import (
    MAX_IMAGE_PIXELS,
    MAX_METADATA_DIMENSION,
    ImageParams,
    Output,
    OutputSpec,
)
from . import OperationError

# Pillow's open-format names. MPO has no opener of its own: the JPEG opener reads it and
# returns an image whose format is "MPO", so it is allowed through "JPEG" (naming "MPO" here
# would make ``Image.open`` raise KeyError for every non-JPEG file).
ALLOWED_FORMATS: Final = ("JPEG", "PNG", "WEBP", "GIF", "HEIF", "AVIF", "BMP", "TIFF")
_DRAFT_FORMATS: Final = frozenset({"JPEG", "MPO"})
_SIXTEEN_BIT_MODES: Final = frozenset({"I", "I;16", "I;16L", "I;16B", "I;16N"})
_MAX_ICC_BYTES: Final = 1 << 20
_CREATE_FLAGS: Final = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY
)
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY
_SRGB: Final = ImageCms.createProfile("sRGB")


def configure(max_pixels: int = MAX_IMAGE_PIXELS) -> None:
    """Process-wide Pillow policy for a tool child; call once before decoding."""
    Image.MAX_IMAGE_PIXELS = max_pixels
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    # Bounded decoder threads keep thread stacks within RLIMIT_AS (see sandbox.py); Pillow's
    # AVIF default (0) means one thread per CPU.
    AvifImagePlugin.DEFAULT_MAX_THREADS = 2
    # No embedded thumbnails, depth or auxiliary images: fewer parsers, one predictable image.
    pillow_heif.register_heif_opener(
        thumbnails=False, depth_images=False, aux_images=False, decode_threads=2,
    )


@contextmanager
def _guarded() -> Iterator[None]:
    """Map decoder refusals to protocol outcomes; anything else propagates as a failure."""
    try:
        yield
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise OperationError("unsupported", "too_large") from None
    except UnidentifiedImageError:
        raise OperationError("failed", "decode_failed") from None


def _checked(image: ImageFile.ImageFile, max_pixels: int) -> ImageFile.ImageFile:
    width, height = image.size
    if (width < 1 or height < 1 or width > MAX_METADATA_DIMENSION
            or height > MAX_METADATA_DIMENSION or width * height > max_pixels):
        raise OperationError("unsupported", "too_large")
    return image


@contextmanager
def open_image(
    descriptor: int, formats: Sequence[str] = ALLOWED_FORMATS,
) -> Iterator[ImageFile.ImageFile]:
    """Open the inherited descriptor from its start with only the allowed decoders."""
    os.lseek(descriptor, 0, os.SEEK_SET)
    with os.fdopen(descriptor, "rb", closefd=False) as stream, _guarded():
        image = Image.open(stream, formats=list(formats))
        try:
            yield _checked(image, Image.MAX_IMAGE_PIXELS or MAX_IMAGE_PIXELS)
        finally:
            image.close()


def _sixteen_to_eight(value: Image.ImagePointTransform) -> Image.ImagePointTransform:
    return value * (1 / 256)


def _normalize(image: Image.Image) -> Image.Image:
    """A mode Pillow resizes well: palettes and bit depths become L, RGB or RGBA."""
    if image.mode in ("RGB", "RGBA", "L", "LA", "CMYK"):
        return image
    if image.mode in _SIXTEEN_BIT_MODES:
        return image.convert("I").point(_sixteen_to_eight).convert("L")
    if image.mode in ("1", "F"):
        return image.convert("L")
    return image.convert("RGBA" if image.has_transparency_data else "RGB")


def _to_srgb(image: Image.Image) -> Image.Image:
    """Convert from an embedded ICC profile to sRGB; a profile lcms refuses is ignored."""
    profile = image.info.get("icc_profile")
    if not isinstance(profile, bytes) or not 0 < len(profile) <= _MAX_ICC_BYTES:
        return image
    mode = "RGBA" if image.mode in ("RGBA", "LA") else "RGB"
    try:
        converted = ImageCms.profileToProfile(
            image, ImageCms.ImageCmsProfile(io.BytesIO(profile)), _SRGB, outputMode=mode,
        )
    except (ImageCms.PyCMSError, OSError, ValueError, TypeError):
        return image
    return image if converted is None else converted


def _final_mode(image: Image.Image) -> Image.Image:
    if image.mode in ("RGB", "RGBA"):
        return image
    return image.convert("RGBA" if image.has_transparency_data else "RGB")


def save_webp(image: Image.Image, directory: str, spec: OutputSpec, quality: int) -> Output:
    """Write ``<directory>/<spec.filename>`` as WebP with no metadata; the file must be new."""
    image.info = {}
    descriptor = os.open(os.path.join(directory, spec.filename), _CREATE_FLAGS, 0o660)
    with open(descriptor, "wb") as stream:
        image.save(stream, format="WEBP", quality=quality, method=4)
        stream.flush()
        size = os.fstat(stream.fileno()).st_size
    width, height = image.size
    return Output(name=spec.name, format=spec.format, bytes=size, width=width, height=height)


def _fit_and_save(
    image: Image.Image, directory: str, spec: OutputSpec, max_edge: int, quality: int,
) -> Output:
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS, reducing_gap=2.0)
    return save_webp(_final_mode(_to_srgb(image)), directory, spec, quality)


def render_photo(
    descriptor: int, directory: str, spec: OutputSpec, params: ImageParams,
) -> Output:
    """A thumbnail or preview of a photo: upright, fit within ``max_edge``, WebP."""
    with open_image(descriptor) as image, _guarded():
        if image.format in _DRAFT_FORMATS:
            image.draft("RGB", (params.max_edge * 2, params.max_edge * 2))
        # pillow-heif already applied the HEIF transforms and reset the EXIF orientation.
        ImageOps.exif_transpose(image, in_place=True)
        return _fit_and_save(_normalize(image), directory, spec, params.max_edge, params.quality)


def render_frame(
    path: str, directory: str, renditions: Sequence[tuple[OutputSpec, int, int]],
) -> tuple[Output, ...]:
    """Poster renditions of a PNG frame extracted by ffmpeg: (spec, max edge, quality) each."""
    descriptor = os.open(path, _READ_FLAGS)
    try:
        with open_image(descriptor, ("PNG",)) as image, _guarded():
            image.load()
            frame = _normalize(image)
            return tuple(
                _fit_and_save(frame.copy(), directory, spec, max_edge, quality)
                for spec, max_edge, quality in renditions
            )
    finally:
        os.close(descriptor)
