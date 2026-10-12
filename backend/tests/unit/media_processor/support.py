"""Synthetic media, roots and jobs for the processor tests; every file is generated at test time."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from aegis_media_processor.child import execute, open_failure
from aegis_media_processor.protocol import (
    Job,
    Lane,
    Limits,
    Op,
    Params,
    Result,
    SourceInput,
    decode_result,
    default_params,
    encode_result,
    media_kind_for_path,
)
from aegis_media_processor.source import (
    InputRefused,
    SourceChanged,
    SourceUnavailable,
    open_source,
)
from PIL import ExifTags, Image

SLOT = "synthetic"
LIMITS = Limits(wall_seconds=60, cpu_seconds=60, memory_bytes=1 << 30, output_bytes=64 << 20)
# Distinctive coordinates: none of these spellings may ever appear in a result.
GPS_LATITUDE = (12.0, 34.0, 56.789)
GPS_LONGITUDE = (98.0, 7.0, 54.321)
COORDINATE_SPELLINGS = (b"56.789", b"54.321", b"12.58", b"98.13", b"56789", b"54321")
RED = (230, 20, 20)
BLUE = (20, 20, 230)


def make_roots(tmp_path: Path) -> Path:
    roots = tmp_path / "roots"
    (roots / SLOT).mkdir(parents=True)
    return roots


def source_path(roots: Path, relative: bytes) -> bytes:
    return os.path.join(os.fsencode(roots), SLOT.encode(), relative)


def write_source(roots: Path, relative: bytes, data: bytes) -> bytes:
    path = source_path(roots, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def source_input(roots: Path, relative: bytes) -> SourceInput:
    observed = os.stat(source_path(roots, relative))
    return SourceInput(
        slot=SLOT, path=relative, device=observed.st_dev, inode=observed.st_ino,
        size=observed.st_size, mtime_ns=observed.st_mtime_ns,
    )


def make_job(
    roots: Path,
    relative: bytes,
    op: Op,
    *,
    params: Params | None = None,
    limits: Limits = LIMITS,
    lane: Lane = "background",
    job_id: str | None = None,
) -> Job:
    kind = media_kind_for_path(relative)
    assert kind is not None
    return Job(
        id=job_id or uuid.uuid4().hex, op=op, kind=kind, lane=lane,
        input=source_input(roots, relative), params=params or default_params(op, kind),
        limits=limits,
    )


def job_directory(tmp_path: Path, job: Job) -> Path:
    """An output directory for an in-process run (the loop's ``work/<id>/out/``)."""
    directory = tmp_path / "work" / job.id / "out"
    directory.mkdir(parents=True)
    return directory


def run_job(tmp_path: Path, roots: Path, job: Job) -> tuple[Result, Path]:
    """What the loop and the child do for one job, in this process and unconfined.

    The loop opens the input (or publishes why it could not); the child decodes from the
    descriptor into its output directory. Every result must be a valid document for its job.
    """
    directory = job_directory(tmp_path, job)
    try:
        descriptor = open_source(job.input, str(roots))
    except (InputRefused, SourceChanged, SourceUnavailable) as error:
        result = open_failure(job, error)
    else:
        try:
            result = execute(job, descriptor, str(directory))
        finally:
            os.close(descriptor)
    assert decode_result(encode_result(result, job), job) == result
    return result, directory


def photo_exif(
    *,
    orientation: int | None = None,
    gps: bool = False,
    make: str | None = "Synthetic Camera Co.",
    model: str | None = "Model 7",
    captured: str | None = "2024:05:01 10:20:30",
    offset: str | None = "+02:00",
) -> bytes:
    exif = Image.Exif()
    if orientation is not None:
        exif[ExifTags.Base.Orientation] = orientation
    if make is not None:
        exif[ExifTags.Base.Make] = make
    if model is not None:
        exif[ExifTags.Base.Model] = model
    if captured is not None or offset is not None:
        details = exif.get_ifd(ExifTags.IFD.Exif)
        if captured is not None:
            details[ExifTags.Base.DateTimeOriginal] = captured
        if offset is not None:
            details[ExifTags.Base.OffsetTimeOriginal] = offset
    if gps:
        location = exif.get_ifd(ExifTags.IFD.GPSInfo)
        location[ExifTags.GPS.GPSLatitudeRef] = "N"
        location[ExifTags.GPS.GPSLatitude] = GPS_LATITUDE
        location[ExifTags.GPS.GPSLongitudeRef] = "E"
        location[ExifTags.GPS.GPSLongitude] = GPS_LONGITUDE
    return exif.tobytes()


def two_colour_image(size: tuple[int, int], mode: str = "RGB") -> Image.Image:
    """Left half red, right half blue, as stored."""
    width, height = size
    image = Image.new(mode, size, BLUE if mode == "RGB" else (*BLUE, 255))
    left = Image.new(mode, (width // 2, height), RED if mode == "RGB" else (*RED, 255))
    image.paste(left, (0, 0))
    return image


def save_image(roots: Path, relative: bytes, image: Image.Image, image_format: str,
               **options: object) -> bytes:
    path = source_path(roots, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    image.save(os.fsdecode(path), format=image_format, **options)
    return path


def dominant(pixel: tuple[int, ...]) -> str:
    red, green, blue = pixel[:3]
    if red > green + 60 and red > blue + 60:
        return "red"
    if blue > red + 60 and blue > green + 60:
        return "blue"
    if green > red + 60 and green > blue + 60:
        return "green"
    return "mixed"
