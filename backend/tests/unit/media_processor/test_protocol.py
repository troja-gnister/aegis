"""The job and result documents: strict, bounded, and the same on both sides of the volume."""

from __future__ import annotations

import base64
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aegis_media_processor import protocol
from aegis_media_processor.protocol import (
    DEFAULT_PARAMS,
    MAX_JOB_BYTES,
    MAX_RESULT_BYTES,
    OP_OUTPUTS,
    POSTER_PARAMS,
    PREVIEW_PARAMS,
    THUMBNAIL_PARAMS,
    TRANSCODE_PARAMS,
    ImageParams,
    Job,
    Limits,
    Metadata,
    Output,
    ProtocolError,
    Result,
    SourceInput,
    TranscodeParams,
    decode_job,
    decode_result,
    encode_job,
    encode_result,
    job_id_from_filename,
    output_magic_matches,
    params_document,
    read_regular_file,
    split_relative_path,
    transcode_wall_seconds,
    write_job,
)

JOB_ID = "0123456789abcdef0123456789abcdef"
LIMITS = Limits(wall_seconds=60, cpu_seconds=60, memory_bytes=1 << 30, output_bytes=64 << 20)
PHOTO = SourceInput("synthetic", b"album/IMG_0001.JPG", 2049, 77, 1234, 1_700_000_000_123_456_789)
VIDEO = SourceInput("synthetic", b"clips/\xffraw name.MOV", 2049, 78, 99, -5)


def job(op: str = "thumbnail", **changes: Any) -> Job:
    source = PHOTO if op in ("thumbnail", "preview", "probe") else VIDEO
    kind = "photo" if source is PHOTO else "video"
    params = DEFAULT_PARAMS[op]  # type: ignore[index]
    base = Job(JOB_ID, op, kind, "background", source, params, LIMITS)  # type: ignore[arg-type]
    return replace(base, **changes)


def document(value: Job | None = None) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(encode_job(value or job()))
    return parsed


def raw(value: object) -> bytes:
    return json.dumps(value).encode()


def metadata(**changes: Any) -> Metadata:
    base = Metadata(
        width=3024, height=4032, orientation=6, captured_at="2024-05-01T10:20:30+02:00",
        captured_at_source="exif", camera_make="Synthetic Camera Co.", camera_model="Model 7",
        duration_ms=None, video_codec=None, audio_codec=None, container=None, has_location=True,
    )
    return replace(base, **changes)


def ok(*outputs: Output, meta: Metadata | None = None) -> Result:
    return Result(JOB_ID, "ok", None, meta, tuple(outputs))


THUMBNAIL = Output("thumbnail", "webp", 2048, 300, 400)


# -- jobs ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("op", ["probe", "thumbnail", "preview", "poster", "transcode"])
def test_jobs_round_trip_for_every_operation(op: str) -> None:
    value = job(op, kind="video", input=VIDEO) if op == "probe" else job(op)

    encoded = encode_job(value)

    assert decode_job(encoded) == value
    assert len(encoded) <= MAX_JOB_BYTES
    assert encoded.isascii()
    assert json.loads(encoded)["version"] == protocol.PROTOCOL_VERSION


def test_raw_path_bytes_travel_as_canonical_base64() -> None:
    parsed = document(job("poster"))

    assert parsed["input"]["path"] == base64.b64encode(b"clips/\xffraw name.MOV").decode()
    assert decode_job(raw(parsed)).input.path == b"clips/\xffraw name.MOV"
    assert list(parsed) == sorted(parsed)


def test_default_params_pin_the_m4_sizes_and_qualities() -> None:
    assert params_document(THUMBNAIL_PARAMS) == {"max_edge": 400, "quality": 75}
    assert params_document(PREVIEW_PARAMS) == {"max_edge": 2048, "quality": 82}
    assert params_document(POSTER_PARAMS) == {
        "thumbnail_edge": 400, "thumbnail_quality": 75, "poster_edge": 2048,
        "poster_quality": 82, "frame_ms": 1000,
    }
    assert params_document(TRANSCODE_PARAMS) == {
        "long_edge": 1920, "short_edge": 1080, "audio_kbps": 160,
    }
    assert params_document(DEFAULT_PARAMS["probe"]) == {}


def test_output_names_and_formats_are_fixed_per_operation() -> None:
    assert {op: [spec.filename for spec in specs] for op, specs in OP_OUTPUTS.items()} == {
        "probe": [],
        "thumbnail": ["thumbnail.webp"],
        "preview": ["preview.webp"],
        "poster": ["thumbnail.webp", "poster.webp"],
        "transcode": ["transcode.mp4"],
    }
    assert OP_OUTPUTS["transcode"][0].content_type == "video/mp4"
    assert OP_OUTPUTS["poster"][1].content_type == "image/webp"


def test_extension_allowlists_match_the_media_app() -> None:
    from aegis_apps.media import formats

    assert protocol.PHOTO_EXTENSIONS == formats.PHOTO_EXTENSIONS
    assert protocol.VIDEO_EXTENSIONS == formats.VIDEO_EXTENSIONS


@pytest.mark.parametrize("slot", ["synthetic", "a", "photos-2024", "a" * 63])
def test_slot_grammar_matches_the_mount_manifests(slot: str) -> None:
    from aegisctl.mounts import SLOT_ID_RE

    assert protocol.SLOT_ID_RE.fullmatch(slot) and SLOT_ID_RE.fullmatch(slot)


@pytest.mark.parametrize("slot", ["", "A", "-a", "1a", "a_b", "a/b", "..", "a" * 64, "ä"])
def test_invalid_slots_are_refused_like_the_mount_manifests(slot: str) -> None:
    from aegisctl.mounts import SLOT_ID_RE

    parsed = document()
    parsed["input"]["slot"] = slot

    assert SLOT_ID_RE.fullmatch(slot) is None
    with pytest.raises(ProtocolError):
        decode_job(raw(parsed))


@pytest.mark.parametrize("path", [
    b"../secret.jpg", b"album/../../secret.jpg", b"/etc/photo.jpg", b"album//photo.jpg",
    b"album/", b"./photo.jpg", b"album/./photo.jpg", b"album/\0photo.jpg", b"",
    b"a" * 256 + b".jpg", b"a/" * 256 + b"photo.jpg", b"a" * 4097,
], ids=lambda path: repr(path[:24]))
def test_path_traversal_and_malformed_paths_are_refused(path: bytes) -> None:
    parsed = document()
    parsed["input"]["path"] = base64.b64encode(path).decode()

    with pytest.raises(ProtocolError):
        split_relative_path(path)
    with pytest.raises(ProtocolError):
        decode_job(raw(parsed))


@pytest.mark.parametrize("encoded", [
    "YWxidW0vcGhvdG8uanBn\n", "YWxidW0vcGhvdG8uanBn====", "YWxidW0vcGhvdG8uanB",
    "YWxidW0vcGhvdG8uanBn-_", "YWxi dW0vcGhvdG8uanBn", "", "!!!!",
])
def test_non_canonical_base64_paths_are_refused(encoded: str) -> None:
    parsed = document()
    parsed["input"]["path"] = encoded

    with pytest.raises(ProtocolError):
        decode_job(raw(parsed))


def _mutations() -> list[tuple[str, Any]]:
    def set_at(keys: tuple[str, ...], value: Any) -> Any:
        def mutate(parsed: dict[str, Any]) -> None:
            target = parsed
            for key in keys[:-1]:
                target = target[key]
            target[keys[-1]] = value
        return mutate

    def delete_at(keys: tuple[str, ...]) -> Any:
        def mutate(parsed: dict[str, Any]) -> None:
            target = parsed
            for key in keys[:-1]:
                target = target[key]
            del target[keys[-1]]
        return mutate

    return [
        ("extra top-level field", set_at(("note",), "x")),
        ("extra input field", set_at(("input", "latitude"), 1)),
        ("extra params field", set_at(("params", "exif"), True)),
        ("extra limits field", set_at(("limits", "nproc"), 1)),
        ("missing id", delete_at(("id",))),
        ("missing input field", delete_at(("input", "inode"))),
        ("missing params field", delete_at(("params", "quality"))),
        ("missing limits field", delete_at(("limits", "output_bytes"))),
        ("version 2", set_at(("version",), 2)),
        ("uppercase id", set_at(("id",), JOB_ID.upper())),
        ("short id", set_at(("id",), JOB_ID[:-1])),
        ("integer id", set_at(("id",), 7)),
        ("unknown op", set_at(("op",), "exec")),
        ("unknown kind", set_at(("kind",), "audio")),
        ("unknown lane", set_at(("lane",), "urgent")),
        ("thumbnail of a video kind", set_at(("kind",), "video")),
        ("string device", set_at(("input", "device"), "2049")),
        ("boolean device", set_at(("input", "device"), True)),
        ("device above 64 bits", set_at(("input", "device"), 1 << 64)),
        ("inode zero", set_at(("input", "inode"), 0)),
        ("negative size", set_at(("input", "size"), -1)),
        ("mtime above 63 bits", set_at(("input", "mtime_ns"), 1 << 63)),
        ("null size", set_at(("input", "size"), None)),
        ("edge above bound", set_at(("params", "max_edge"), 4097)),
        ("edge below bound", set_at(("params", "max_edge"), 15)),
        ("quality zero", set_at(("params", "quality"), 0)),
        ("boolean quality", set_at(("params", "quality"), True)),
        ("transcode params on a thumbnail", set_at(("params",), {
            "long_edge": 1920, "short_edge": 1080, "audio_kbps": 160})),
        ("params list", set_at(("params",), [400, 75])),
        ("wall zero", set_at(("limits", "wall_seconds"), 0)),
        ("wall above a day", set_at(("limits", "wall_seconds"), 86_401)),
        ("memory below bound", set_at(("limits", "memory_bytes"), 1 << 20)),
        ("memory above bound", set_at(("limits", "memory_bytes"), 17 << 30)),
        ("output below bound", set_at(("limits", "output_bytes"), 1024)),
        ("input list", set_at(("input",), [])),
    ]


@pytest.mark.parametrize("name,mutate", _mutations(), ids=[name for name, _ in _mutations()])
def test_hostile_job_fields_are_refused(name: str, mutate: Any) -> None:
    parsed = document()
    mutate(parsed)

    with pytest.raises(ProtocolError):
        decode_job(raw(parsed))


def test_kind_must_match_the_extension_and_the_operation() -> None:
    wrong_extension = replace(PHOTO, path=b"album/notes.txt")

    for value in (
        job("thumbnail", input=VIDEO, kind="video"),
        job("poster", input=PHOTO, kind="photo"),
        job("probe", input=wrong_extension),
        job("transcode", input=VIDEO, kind="photo"),
    ):
        with pytest.raises(ProtocolError):
            encode_job(value)
    with pytest.raises(ProtocolError):
        decode_job(raw(document() | {"kind": "video"}))


@pytest.mark.parametrize("payload", [
    b"", b"{", b"[]", b'"job"', b"1", b"null", b"[" * 10_000 + b"]" * 10_000,
    b'{"version": 1, "version": 1}', b"\xff\xfe{}", b'{"version": NaN}',
    b'{"version": Infinity}', b'{"version": 1.0}', b'{"version": 1' + b"0" * 30 + b"}",
], ids=lambda payload: repr(payload[:20]))
def test_malformed_job_json_is_refused(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_job(payload)


def test_duplicate_keys_in_a_valid_job_are_refused() -> None:
    encoded = encode_job(job())
    duplicated = encoded.replace(b'"op":"thumbnail"', b'"op":"thumbnail","op":"preview"', 1)

    assert duplicated != encoded
    with pytest.raises(ProtocolError):
        decode_job(duplicated)


def test_oversize_job_documents_are_refused_before_parsing() -> None:
    encoded = encode_job(job())

    assert decode_job(encoded + b" " * (MAX_JOB_BYTES - len(encoded))) == job()
    with pytest.raises(ProtocolError):
        decode_job(encoded + b" " * (MAX_JOB_BYTES - len(encoded) + 1))


def test_encoding_validates_the_objects_too() -> None:
    for value in (
        job(id="not-an-id"),
        job(input=replace(PHOTO, device=True)),
        job(input=replace(PHOTO, path="album/photo.jpg")),  # type: ignore[arg-type]
        job(params=ImageParams(max_edge=400.0, quality=75)),  # type: ignore[arg-type]
        job(params=TRANSCODE_PARAMS),
        job("transcode", params=TranscodeParams(long_edge=1080, short_edge=1920, audio_kbps=160)),
        job(limits=replace(LIMITS, cpu_seconds=0)),
    ):
        with pytest.raises(ProtocolError):
            encode_job(value)


# -- results -------------------------------------------------------------------------------------


def test_results_round_trip_and_bind_to_their_job() -> None:
    probe_job = job("probe")
    probe = ok(meta=metadata())
    thumbnail = ok(THUMBNAIL)
    poster = ok(Output("thumbnail", "webp", 10, 400, 225), Output("poster", "webp", 20, 2048, 1152))
    transcode = ok(Output("transcode", "mp4", 1 << 20, 1080, 1920))

    assert decode_result(encode_result(probe, probe_job), probe_job) == probe
    assert decode_result(encode_result(thumbnail, job()), job()) == thumbnail
    assert decode_result(encode_result(poster, job("poster")), job("poster")) == poster
    assert decode_result(encode_result(transcode, job("transcode")), job("transcode")) == transcode
    for status, code in (("unsupported", "too_large"), ("changed", "source_changed"),
                         ("failed", "decode_failed"), ("timeout", "timeout")):
        failure = Result(JOB_ID, status, code, None, ())  # type: ignore[arg-type]
        assert decode_result(encode_result(failure, job()), job()) == failure


def test_display_dimensions_are_documented_and_carried_as_given() -> None:
    assert "displayed" in (Metadata.__doc__ or "")
    portrait = metadata(width=3024, height=4032, orientation=6)

    decoded = decode_result(encode_result(ok(meta=portrait), job("probe")), job("probe"))

    assert decoded.metadata is not None
    assert (decoded.metadata.width, decoded.metadata.height) == (3024, 4032)
    assert decoded.metadata.orientation == 6


def result_document(result: Result, value: Job | None = None) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(encode_result(result, value))
    return parsed


@pytest.mark.parametrize("change", [
    {"gps_latitude": 12.5},
    {"latitude": "12.58"},
    {"location": "+12.5824+098.1317/"},
], ids=["gps-float", "latitude-string", "location-string"])
def test_no_metadata_field_can_carry_coordinates(change: dict[str, Any]) -> None:
    parsed = result_document(ok(meta=metadata()), job("probe"))
    parsed["metadata"].update(change)

    with pytest.raises(ProtocolError):
        decode_result(raw(parsed), job("probe"))


def _metadata_mutations() -> list[tuple[str, dict[str, Any]]]:
    return [
        ("float width", {"width": 3024.0}),
        ("width without height", {"height": None}),
        ("zero width", {"width": 0}),
        ("orientation 9", {"orientation": 9}),
        ("captured month 13", {"captured_at": "2024-13-01T10:00:00"}),
        ("captured with space", {"captured_at": "2024-05-01 10:00:00"}),
        ("captured offset 25h", {"captured_at": "2024-05-01T10:00:00+25:00"}),
        ("captured year 1700", {"captured_at": "1700-05-01T10:00:00"}),
        ("captured fraction", {"captured_at": "2024-05-01T10:00:00.5Z"}),
        ("captured without source", {"captured_at_source": None}),
        ("source without captured", {"captured_at": None}),
        ("unknown source", {"captured_at_source": "filename"}),
        ("control character", {"camera_make": "Cam\x07"}),
        ("leading space", {"camera_make": " Cam"}),
        ("bidi override", {"camera_model": "Model ‮7"}),
        ("65 characters", {"camera_model": "m" * 65}),
        ("empty camera", {"camera_make": ""}),
        ("codec with dot", {"video_codec": "h.264"}),
        ("uppercase codec", {"audio_codec": "AAC"}),
        ("unknown container", {"container": "flv"}),
        ("integer location flag", {"has_location": 1}),
        ("missing location flag", {"has_location": None}),
        ("photo with duration", {"duration_ms": 1000}),
        ("photo with container", {"container": "mp4"}),
    ]


@pytest.mark.parametrize(
    "name,change", _metadata_mutations(), ids=[name for name, _ in _metadata_mutations()],
)
def test_hostile_metadata_is_refused(name: str, change: dict[str, Any]) -> None:
    parsed = result_document(ok(meta=metadata()), job("probe"))
    parsed["metadata"].update(change)

    with pytest.raises(ProtocolError):
        decode_result(raw(parsed), job("probe"))


@pytest.mark.parametrize("captured", [
    "2024-05-01T10:20:30", "2024-05-01T10:20:30Z", "2024-05-01T10:20:30+02:00",
    "2024-05-01T10:20:30-09:30", "1899-12-31T23:59:59",
])
def test_capture_times_with_and_without_an_offset_are_accepted(captured: str) -> None:
    meta = metadata(captured_at=captured, captured_at_source="exif")

    assert decode_result(encode_result(ok(meta=meta), job("probe")), job("probe")).metadata == meta


def test_video_metadata_has_no_exif_fields() -> None:
    video_probe = job("probe", input=VIDEO, kind="video")
    video = metadata(orientation=None, captured_at_source="container", duration_ms=12_345,
                     video_codec="hevc", audio_codec="aac", container="mov",
                     camera_make=None, camera_model=None)

    assert decode_result(encode_result(ok(meta=video), video_probe), video_probe).metadata == video
    with pytest.raises(ProtocolError):
        encode_result(ok(meta=replace(video, orientation=6)), video_probe)
    with pytest.raises(ProtocolError):
        encode_result(ok(meta=replace(video, captured_at_source="exif")), video_probe)


def _result_mutations() -> list[tuple[str, Any, Job]]:
    def edit(**fields: Any) -> Any:
        def mutate(parsed: dict[str, Any]) -> None:
            parsed.update(fields)
        return mutate

    def edit_output(**fields: Any) -> Any:
        def mutate(parsed: dict[str, Any]) -> None:
            parsed["outputs"][0].update(fields)
        return mutate

    def add_output(output: dict[str, Any]) -> Any:
        def mutate(parsed: dict[str, Any]) -> None:
            parsed["outputs"].append(output)
        return mutate

    thumbnail_job = job()
    return [
        ("ok with a code", edit(safe_error_code="decode_failed"), thumbnail_job),
        ("failed without a code", edit(status="failed", outputs=[]), thumbnail_job),
        ("failed with a timeout code",
         edit(status="failed", safe_error_code="timeout", outputs=[]), thumbnail_job),
        ("failed with exception text",
         edit(status="failed", safe_error_code="OSError: /srv/aegis/roots/x", outputs=[]),
         thumbnail_job),
        ("failed with outputs", edit(status="failed", safe_error_code="decode_failed"),
         thumbnail_job),
        ("unknown status", edit(status="partial"), thumbnail_job),
        ("other job id", edit(id="f" * 32), thumbnail_job),
        ("missing output", edit(outputs=[]), thumbnail_job),
        ("output of another operation", edit_output(name="preview"), thumbnail_job),
        ("format mismatch", edit_output(format="mp4"), thumbnail_job),
        ("path in the name", edit_output(name="../thumbnail"), thumbnail_job),
        ("empty output", edit_output(bytes=0), thumbnail_job),
        ("output above the job limit", edit_output(bytes=(64 << 20) + 1), thumbnail_job),
        ("output wider than the edge", edit_output(width=401), thumbnail_job),
        ("float dimension", edit_output(height=400.0), thumbnail_job),
        ("duplicate output", add_output({"name": "thumbnail", "format": "webp", "bytes": 1,
                                         "width": 1, "height": 1}), thumbnail_job),
        ("extra output field", edit_output(path="/tmp/x.webp"), thumbnail_job),
        ("metadata on a thumbnail", edit(metadata=json.loads(
            encode_result(ok(meta=metadata()), job("probe")))["metadata"]), thumbnail_job),
        ("extra result field", edit(stderr="private"), thumbnail_job),
        ("version 2", edit(version=2), thumbnail_job),
        ("outputs object", edit(outputs={}), thumbnail_job),
    ]


@pytest.mark.parametrize(
    "name,mutate,bound", _result_mutations(), ids=[name for name, _, _ in _result_mutations()],
)
def test_hostile_results_are_refused(name: str, mutate: Any, bound: Job) -> None:
    parsed = result_document(ok(THUMBNAIL), bound)
    mutate(parsed)

    with pytest.raises(ProtocolError):
        decode_result(raw(parsed), bound)


def test_transcode_bounds_follow_the_long_and_short_edges() -> None:
    transcode_job = job("transcode")

    for width, height in ((1920, 1080), (1080, 1920), (640, 480)):
        result = ok(Output("transcode", "mp4", 100, width, height))
        assert decode_result(encode_result(result, transcode_job), transcode_job) == result
    for width, height in ((1920, 1920), (2160, 1080), (1080, 2160)):
        with pytest.raises(ProtocolError):
            encode_result(ok(Output("transcode", "mp4", 100, width, height)), transcode_job)


def test_probe_results_require_metadata_and_no_outputs() -> None:
    with pytest.raises(ProtocolError):
        encode_result(ok(), job("probe"))
    with pytest.raises(ProtocolError):
        encode_result(ok(THUMBNAIL, meta=metadata()), job("probe"))


def test_oversize_result_documents_are_refused() -> None:
    encoded = encode_result(ok(THUMBNAIL), job())

    assert decode_result(encoded + b"\n" * (MAX_RESULT_BYTES - len(encoded)), job())
    with pytest.raises(ProtocolError):
        decode_result(encoded + b"\n" * (MAX_RESULT_BYTES - len(encoded) + 1), job())


def test_results_decode_without_their_job_under_general_bounds() -> None:
    decoded = decode_result(encode_result(ok(THUMBNAIL)))

    assert decoded == ok(THUMBNAIL)
    with pytest.raises(ProtocolError):
        encode_result(ok(Output("thumbnail", "webp", 1, 16_385, 1)))


# -- helpers and files ---------------------------------------------------------------------------


def test_inbox_names_are_job_ids_and_nothing_else() -> None:
    assert job_id_from_filename(JOB_ID + ".json") == JOB_ID
    for name in (f".{JOB_ID}.json.0123.tmp", JOB_ID, JOB_ID + ".JSON", JOB_ID.upper() + ".json",
                 "../" + JOB_ID + ".json", JOB_ID + "0.json"):
        assert job_id_from_filename(name) is None


def test_output_signatures() -> None:
    assert output_magic_matches("webp", b"RIFF\x10\x00\x00\x00WEBPVP8 ")
    assert output_magic_matches("mp4", b"\x00\x00\x00\x20ftypisom")
    assert not output_magic_matches("webp", b"\x00\x00\x00\x20ftypisom")
    assert not output_magic_matches("mp4", b"RIFF\x10\x00\x00\x00WEBPVP8 ")
    assert not output_magic_matches("mp4", b"ftyp")


def test_transcode_wall_budget_scales_with_duration_and_is_capped() -> None:
    assert transcode_wall_seconds(None, 7200) == 7200
    assert transcode_wall_seconds(1000, 7200) == 120
    assert transcode_wall_seconds(600_000, 7200) == 1860
    assert transcode_wall_seconds(10 * 3600 * 1000, 7200) == 7200
    with pytest.raises(ProtocolError):
        transcode_wall_seconds(-1, 7200)
    with pytest.raises(ProtocolError):
        transcode_wall_seconds(1000, 0)


def test_write_job_never_replaces_a_queued_job_and_leaves_no_temporary_file(
    tmp_path: Path,
) -> None:
    write_job(str(tmp_path), job())

    assert os.listdir(tmp_path) == [JOB_ID + ".json"]
    assert decode_job((tmp_path / (JOB_ID + ".json")).read_bytes()) == job()
    with pytest.raises(FileExistsError):
        write_job(str(tmp_path), job(lane="interactive"))
    assert os.listdir(tmp_path) == [JOB_ID + ".json"]
    assert decode_job((tmp_path / (JOB_ID + ".json")).read_bytes()).lane == "background"


def test_bounded_reads_refuse_symlinks_fifos_and_oversize_files(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(b"x" * 10)
    (tmp_path / "link.json").symlink_to(target)
    os.mkfifo(tmp_path / "fifo.json")

    assert read_regular_file(str(target), 10) == b"x" * 10
    with pytest.raises(ProtocolError):
        read_regular_file(str(target), 9)
    with pytest.raises(OSError):
        read_regular_file(str(tmp_path / "link.json"), 10)
    with pytest.raises(ProtocolError):
        read_regular_file(str(tmp_path / "fifo.json"), 10)
    assert stat.S_ISFIFO(os.lstat(tmp_path / "fifo.json").st_mode)
