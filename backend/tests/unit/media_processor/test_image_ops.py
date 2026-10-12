"""Photo probes, thumbnails and previews against fixtures generated with Pillow (M4, M5)."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pillow_heif
import pytest
from aegis_media_processor.child import execute
from aegis_media_processor.ops import OperationError, image
from aegis_media_processor.ops.probe import camera_text, exif_captured_at
from aegis_media_processor.protocol import (
    PREVIEW_PARAMS,
    THUMBNAIL_PARAMS,
    Metadata,
    OutputSpec,
    encode_result,
    output_magic_matches,
)
from aegis_media_processor.source import open_source
from PIL import Image

from .support import (
    COORDINATE_SPELLINGS,
    dominant,
    job_directory,
    make_job,
    make_roots,
    photo_exif,
    run_job,
    save_image,
    two_colour_image,
    write_source,
)

METADATA_FIELDS = {
    "width", "height", "orientation", "captured_at", "captured_at_source", "camera_make",
    "camera_model", "duration_ms", "video_codec", "audio_codec", "container", "has_location",
}


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    return make_roots(tmp_path)


def probe(tmp_path: Path, roots: Path, relative: bytes) -> tuple[Metadata, bytes]:
    # A job id without digits, so no coordinate spelling can appear in it by chance.
    job = make_job(roots, relative, "probe", job_id="a" * 32)
    result, directory = run_job(tmp_path, roots, job)
    assert result.status == "ok", result
    assert result.metadata is not None
    assert os.listdir(directory) == []
    return result.metadata, encode_result(result, job)


def open_output(directory: Path, name: str) -> Image.Image:
    path = directory / name
    with path.open("rb") as handle:
        assert output_magic_matches("webp", handle.read(12))
    output = Image.open(path)
    assert output.format == "WEBP"
    return output


# -- probes --------------------------------------------------------------------------------------


def test_probe_reports_display_dimensions_exif_fields_and_only_location_presence(
    tmp_path: Path, roots: Path,
) -> None:
    exif = photo_exif(orientation=6, gps=True)
    save_image(roots, b"album/IMG_0001.JPG", two_colour_image((64, 32)), "JPEG",
               exif=exif, quality=95)

    metadata, encoded = probe(tmp_path, roots, b"album/IMG_0001.JPG")

    # Stored 64x32 with orientation 6 (rotate 90 degrees to display): displayed 32x64.
    assert (metadata.width, metadata.height) == (32, 64)
    assert metadata.orientation == 6
    assert metadata.captured_at == "2024-05-01T10:20:30+02:00"
    assert metadata.captured_at_source == "exif"
    assert metadata.camera_make == "Synthetic Camera Co."
    assert metadata.camera_model == "Model 7"
    assert metadata.has_location is True
    assert (metadata.duration_ms, metadata.video_codec, metadata.audio_codec,
            metadata.container) == (None, None, None, None)
    assert set(Metadata.__dataclass_fields__) == METADATA_FIELDS
    lowered = encoded.lower()
    assert b"gps" not in lowered and b"latitude" not in lowered and b"longitude" not in lowered
    assert not any(spelling in encoded for spelling in COORDINATE_SPELLINGS)


@pytest.mark.parametrize("orientation,displayed", [
    (1, (64, 32)), (3, (64, 32)), (5, (32, 64)), (8, (32, 64)), (None, (64, 32)),
])
def test_only_orientations_5_to_8_swap_the_displayed_dimensions(
    tmp_path: Path, roots: Path, orientation: int | None, displayed: tuple[int, int],
) -> None:
    save_image(roots, b"photo.jpg", two_colour_image((64, 32)), "JPEG",
               exif=photo_exif(orientation=orientation))

    metadata, _ = probe(tmp_path, roots, b"photo.jpg")

    assert (metadata.width, metadata.height) == displayed
    assert metadata.orientation == orientation


def test_probe_without_exif_has_no_capture_time_camera_or_location(
    tmp_path: Path, roots: Path,
) -> None:
    save_image(roots, b"plain.png", Image.new("RGB", (120, 80), (1, 2, 3)), "PNG")

    metadata, _ = probe(tmp_path, roots, b"plain.png")

    assert (metadata.width, metadata.height, metadata.orientation) == (120, 80, None)
    assert metadata.captured_at is None and metadata.captured_at_source is None
    assert metadata.camera_make is None and metadata.camera_model is None
    assert metadata.has_location is False


def test_capture_time_without_an_offset_stays_local(tmp_path: Path, roots: Path) -> None:
    save_image(roots, b"local.jpg", Image.new("RGB", (8, 8)), "JPEG",
               exif=photo_exif(offset=None))

    metadata, _ = probe(tmp_path, roots, b"local.jpg")

    assert metadata.captured_at == "2024-05-01T10:20:30"


def test_unset_or_garbage_exif_values_are_dropped(tmp_path: Path, roots: Path) -> None:
    exif = photo_exif(captured="0000:00:00 00:00:00", make="\x00\x00 ", model="Cam\x07era\n")
    save_image(roots, b"garbage.jpg", Image.new("RGB", (8, 8)), "JPEG", exif=exif)

    metadata, _ = probe(tmp_path, roots, b"garbage.jpg")

    assert metadata.captured_at is None
    assert metadata.camera_make is None
    assert metadata.camera_model == "Camera"


def test_exif_text_helpers_bound_and_clean_values() -> None:
    assert camera_text("  Canon\x00\x00 ") == "Canon"
    assert camera_text(b"NIKON CORPORATION\x00") == "NIKON CORPORATION"
    assert camera_text("a" * 100) == "a" * 64
    assert camera_text("x‮y") == "xy"
    assert camera_text(17) is None
    assert exif_captured_at("2024:02:29 23:59:59\x00", "-05:00") == "2024-02-29T23:59:59-05:00"
    assert exif_captured_at("2023:02:29 23:59:59", None) is None
    assert exif_captured_at("2024:05:01 10:20:30", "+99:00") == "2024-05-01T10:20:30"
    assert exif_captured_at(b"2024:05:01 10:20:30", b"+01:00") == "2024-05-01T10:20:30+01:00"


def test_heic_probe_and_thumbnail_through_pillow_heif(tmp_path: Path, roots: Path) -> None:
    pillow_heif.register_heif_opener()
    save_image(roots, b"phone/IMG_0002.HEIC", Image.new("RGB", (640, 480), (200, 100, 50)),
               "HEIF", quality=50, exif=photo_exif(gps=True))

    metadata, encoded = probe(tmp_path, roots, b"phone/IMG_0002.HEIC")
    result, directory = run_job(
        tmp_path, roots, make_job(roots, b"phone/IMG_0002.HEIC", "thumbnail"),
    )

    assert (metadata.width, metadata.height) == (640, 480)
    assert metadata.has_location is True
    assert not any(spelling in encoded for spelling in COORDINATE_SPELLINGS)
    assert result.status == "ok", result
    assert [(o.name, o.width, o.height) for o in result.outputs] == [("thumbnail", 400, 300)]
    rendered = open_output(directory, "thumbnail.webp")
    assert rendered.size == (400, 300)
    assert not {"exif", "xmp", "icc_profile"} & set(rendered.info)


# -- renderings ----------------------------------------------------------------------------------


def test_thumbnail_applies_the_exif_orientation_and_writes_no_metadata(
    tmp_path: Path, roots: Path,
) -> None:
    save_image(roots, b"IMG_0001.jpg", two_colour_image((64, 32)), "JPEG",
               exif=photo_exif(orientation=6, gps=True), quality=95)
    job = make_job(roots, b"IMG_0001.jpg", "thumbnail")

    result, directory = run_job(tmp_path, roots, job)

    assert result.status == "ok", result
    (output,) = result.outputs
    assert (output.name, output.format, output.width, output.height) == (
        "thumbnail", "webp", 32, 64,
    )
    assert output.bytes == (directory / "thumbnail.webp").stat().st_size
    assert os.listdir(directory) == ["thumbnail.webp"]
    rendered = open_output(directory, "thumbnail.webp").convert("RGB")
    assert rendered.size == (32, 64)
    # The stored left half (red) is the displayed top after rotating 90 degrees clockwise.
    assert dominant(rendered.getpixel((16, 8))) == "red"  # type: ignore[arg-type]
    assert dominant(rendered.getpixel((16, 56))) == "blue"  # type: ignore[arg-type]
    written = open_output(directory, "thumbnail.webp").info
    assert not {"exif", "xmp", "icc_profile"} & set(written)


def test_thumbnail_fits_within_400_and_preview_within_2048(tmp_path: Path, roots: Path) -> None:
    save_image(roots, b"wide.png", Image.new("RGB", (3000, 1000), (0, 90, 30)), "PNG")
    save_image(roots, b"small.png", Image.new("RGB", (1200, 800), (0, 90, 30)), "PNG")

    sizes = {}
    for relative, op in ((b"wide.png", "thumbnail"), (b"wide.png", "preview"),
                         (b"small.png", "thumbnail"), (b"small.png", "preview")):
        result, _ = run_job(tmp_path, roots, make_job(roots, relative, op))  # type: ignore[arg-type]
        assert result.status == "ok", result
        sizes[(relative, op)] = (result.outputs[0].width, result.outputs[0].height)

    assert sizes[(b"wide.png", "thumbnail")] == (400, 133)
    assert sizes[(b"wide.png", "preview")] == (2048, 683)
    assert sizes[(b"small.png", "thumbnail")][0] == 400
    assert sizes[(b"small.png", "thumbnail")][1] in (266, 267)
    # Never enlarged: 1200x800 already fits within 2048.
    assert sizes[(b"small.png", "preview")] == (1200, 800)


def test_transparency_is_kept(tmp_path: Path, roots: Path) -> None:
    save_image(roots, b"alpha.png", Image.new("RGBA", (300, 200), (0, 128, 255, 100)), "PNG")

    result, directory = run_job(tmp_path, roots, make_job(roots, b"alpha.png", "thumbnail"))

    assert result.status == "ok", result
    assert open_output(directory, "thumbnail.webp").mode == "RGBA"


def test_animated_gif_uses_its_first_frame(tmp_path: Path, roots: Path) -> None:
    frames = [Image.new("RGB", (40, 40), colour) for colour in ((230, 20, 20), (20, 230, 20))]
    path = save_image(roots, b"animated.gif", frames[0], "GIF", save_all=True,
                      append_images=frames[1:], duration=100, loop=0)
    with Image.open(os.fsdecode(path)) as written:
        assert getattr(written, "n_frames", 1) == 2

    result, directory = run_job(tmp_path, roots, make_job(roots, b"animated.gif", "thumbnail"))

    assert result.status == "ok", result
    rendered = open_output(directory, "thumbnail.webp").convert("RGB")
    assert dominant(rendered.getpixel((20, 20))) == "red"  # type: ignore[arg-type]


def test_a_multi_picture_jpeg_opens_through_the_jpeg_decoder(tmp_path: Path, roots: Path) -> None:
    frames = [Image.new("RGB", (40, 40), colour) for colour in ((230, 20, 20), (20, 230, 20))]
    path = save_image(roots, b"stereo.jpg", frames[0], "MPO", save_all=True,
                      append_images=frames[1:])
    with Image.open(os.fsdecode(path)) as written:
        assert written.format == "MPO"

    result, directory = run_job(tmp_path, roots, make_job(roots, b"stereo.jpg", "thumbnail"))

    assert result.status == "ok", result
    rendered = open_output(directory, "thumbnail.webp").convert("RGB")
    assert dominant(rendered.getpixel((20, 20))) == "red"  # type: ignore[arg-type]


def test_palette_and_sixteen_bit_images_render(tmp_path: Path, roots: Path) -> None:
    save_image(roots, b"palette.png", Image.new("RGB", (50, 50), (10, 200, 10)).convert("P"),
               "PNG")
    save_image(roots, b"deep.png", Image.new("I;16", (50, 50), 40_000), "PNG")

    for relative in (b"palette.png", b"deep.png"):
        result, directory = run_job(tmp_path, roots, make_job(roots, relative, "preview"))
        assert result.status == "ok", result
        assert open_output(directory, "preview.webp").size == (50, 50)


# -- outcomes ------------------------------------------------------------------------------------


def test_a_changed_fingerprint_reports_changed_without_decoding(
    tmp_path: Path, roots: Path,
) -> None:
    save_image(roots, b"photo.jpg", Image.new("RGB", (16, 16)), "JPEG")
    job = make_job(roots, b"photo.jpg", "thumbnail")
    stale = replace(job, id="b" * 32, input=replace(job.input, mtime_ns=job.input.mtime_ns + 1))

    result, directory = run_job(tmp_path, roots, stale)
    assert (result.status, result.safe_error_code) == ("changed", "source_changed")
    assert os.listdir(directory) == []

    write_source(roots, b"photo.jpg", b"rewritten, and no longer an image at all")
    result, directory = run_job(tmp_path, roots, job)
    assert (result.status, result.safe_error_code) == ("changed", "source_changed")
    assert os.listdir(directory) == []


def test_a_corrupt_file_fails(tmp_path: Path, roots: Path) -> None:
    write_source(roots, b"corrupt.jpg", b"this is not an image " * 100)

    for op in ("probe", "thumbnail"):
        result, _ = run_job(tmp_path, roots, make_job(roots, b"corrupt.jpg", op))
        assert (result.status, result.safe_error_code) == ("failed", "decode_failed")


def test_a_truncated_jpeg_fails_to_render(tmp_path: Path, roots: Path) -> None:
    noise = Image.frombytes("RGB", (256, 256), os.urandom(256 * 256 * 3))
    path = save_image(roots, b"truncated.jpg", noise, "JPEG", quality=95)
    data = Path(os.fsdecode(path)).read_bytes()
    Path(os.fsdecode(path)).write_bytes(data[: len(data) // 2])

    result, directory = run_job(tmp_path, roots, make_job(roots, b"truncated.jpg", "thumbnail"))

    assert (result.status, result.safe_error_code) == ("failed", "decode_failed")
    assert result.outputs == ()
    assert "thumbnail.webp" not in os.listdir(directory)


def test_a_misnamed_file_never_reaches_another_decoder(tmp_path: Path, roots: Path) -> None:
    save_image(roots, b"misnamed.jpg", Image.new("RGB", (16, 16)), "PPM")

    result, _ = run_job(tmp_path, roots, make_job(roots, b"misnamed.jpg", "thumbnail"))

    assert (result.status, result.safe_error_code) == ("failed", "decode_failed")


def test_images_above_the_pixel_limit_are_unsupported(
    tmp_path: Path, roots: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS)
    save_image(roots, b"big.png", Image.new("RGB", (100, 100)), "PNG")
    job = make_job(roots, b"big.png", "thumbnail")
    directory = job_directory(tmp_path, job)
    descriptor = os.open(os.path.join(os.fsencode(roots), b"synthetic", b"big.png"), os.O_RDONLY)
    try:
        for max_pixels in (9_999, 4_000):  # just over the limit (warning), and over twice it
            image.configure(max_pixels=max_pixels)
            with pytest.raises(OperationError) as refused:
                image.render_photo(descriptor, str(directory), OutputSpec("thumbnail", "webp"),
                                   THUMBNAIL_PARAMS)
            assert (refused.value.status, refused.value.code) == ("unsupported", "too_large")
    finally:
        os.close(descriptor)
        image.configure()
    assert os.listdir(directory) == []


def test_a_missing_slot_is_unavailable(tmp_path: Path, roots: Path) -> None:
    save_image(roots, b"photo.jpg", Image.new("RGB", (16, 16)), "JPEG")
    job = make_job(roots, b"photo.jpg", "preview", params=PREVIEW_PARAMS)
    (roots / "synthetic").rename(roots / "elsewhere")

    result, _ = run_job(tmp_path, roots, job)

    assert (result.status, result.safe_error_code) == ("failed", "source_unavailable")


def test_an_existing_output_is_never_overwritten(tmp_path: Path, roots: Path) -> None:
    save_image(roots, b"photo.jpg", Image.new("RGB", (16, 16)), "JPEG")
    job = make_job(roots, b"photo.jpg", "thumbnail")
    directory = job_directory(tmp_path, job)
    (directory / "thumbnail.webp").symlink_to(tmp_path / "elsewhere.webp")
    descriptor = open_source(job.input, str(roots))
    try:
        result = execute(job, descriptor, str(directory))
    finally:
        os.close(descriptor)

    assert result.status == "failed"
    assert not (tmp_path / "elsewhere.webp").exists()


def test_the_child_decodes_only_the_descriptor_it_was_given(tmp_path: Path, roots: Path) -> None:
    save_image(roots, b"photo.jpg", Image.new("RGB", (16, 16)), "JPEG")
    save_image(roots, b"other.jpg", Image.new("RGB", (32, 32)), "JPEG")
    job = make_job(roots, b"photo.jpg", "thumbnail")
    directory = job_directory(tmp_path, job)
    other = open_source(make_job(roots, b"other.jpg", "thumbnail").input, str(roots))
    try:
        # Another file's descriptor is refused before any decoding.
        assert execute(job, other, str(directory)).safe_error_code == "source_changed"
    finally:
        os.close(other)
    assert os.listdir(directory) == []


def test_a_file_edited_while_decoding_reports_changed(
    tmp_path: Path, roots: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = save_image(roots, b"photo.jpg", Image.new("RGB", (16, 16)), "JPEG")
    job = make_job(roots, b"photo.jpg", "thumbnail")
    directory = job_directory(tmp_path, job)
    original = image.render_photo

    def render_then_edit(*arguments: object, **options: object) -> object:
        output = original(*arguments, **options)  # type: ignore[arg-type]
        with open(path, "ab") as handle:
            handle.write(b"appended during decoding")
        return output

    monkeypatch.setattr(image, "render_photo", render_then_edit)
    descriptor = open_source(job.input, str(roots))
    try:
        result = execute(job, descriptor, str(directory))
    finally:
        os.close(descriptor)

    assert (result.status, result.safe_error_code, result.outputs) == (
        "changed", "source_changed", (),
    )


def test_avif_decodes_with_bounded_threads(tmp_path: Path, roots: Path) -> None:
    from PIL import AvifImagePlugin

    save_image(roots, b"photo.avif", Image.new("RGB", (640, 480), (30, 160, 90)), "AVIF",
               quality=50)

    result, directory = run_job(tmp_path, roots, make_job(roots, b"photo.avif", "thumbnail"))

    assert result.status == "ok", result
    assert AvifImagePlugin.DEFAULT_MAX_THREADS == 2
    assert open_output(directory, "thumbnail.webp").size == (400, 300)
