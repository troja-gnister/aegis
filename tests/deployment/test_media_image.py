"""The built aegis-media image (M2, M8): identity, tools, an offline processor run, video tests.

The module builds docker/media.Dockerfile twice from the working tree (the published default
target and the never-published ``media-test`` target), each under a run-unique tag and owner
label, and removes exactly those images afterwards. Every container is disposable: ``--rm``, a
run-unique name and label, no network, read-only root, no capabilities, and an exact-identity
removal in ``finally``.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from aegisctl.container_engine import container_command

from tests.support.container_runtime import copy_bind_inputs
from tests.support.container_runtime import run_deployment_process as run_container

REPOSITORY = Path(__file__).resolve().parents[2]
CONTAINER_COMMAND = container_command()
DOCKERFILE = "docker/media.Dockerfile"
OWNER_LABEL = "aegis.verify.owner"
BUILD_TIMEOUT = 1800
# The decoders of the legacy video formats (3GP/3G2, WMV/ASF, FLV, MPG/VOB, OGV, DivX). Debian's
# ffmpeg must keep them: the formats are on the M4 allowlist.
LEGACY_DECODERS = (
    "h263", "msmpeg4", "wmv1", "wmv2", "wmv3", "vc1", "flv", "vp6f", "mpeg1video", "mpeg2video",
    "theora", "amrnb", "amrwb", "wmav1", "wmav2", "wmapro", "mp2", "ac3", "vorbis",
)
NETWORK_CLIENTS = (
    "curl", "wget", "nc", "ncat", "netcat", "socat", "ssh", "scp", "sftp", "telnet", "ftp",
    "rsync",
)
CONTAINMENT = (
    "--network", "none", "--read-only", "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges:true", "--pids-limit", "256", "--memory", "4g",
    "--tmpfs", "/tmp:size=512m,mode=1777",
)

# Run in the published image as its own user: the processor serves real jobs offline.
PROCESSOR_RUN = r'''
import json, os, signal, subprocess, sys, time, uuid
from PIL import Image
from aegis_media_processor import protocol as p

roots, scratch = "/tmp/roots", "/tmp/scratch"
slot = os.path.join(roots, "synthetic")
os.makedirs(slot)
os.makedirs(scratch)
Image.new("RGB", (1200, 800), (10, 120, 200)).save(os.path.join(slot, "photo.jpg"), quality=90)
subprocess.run(
    ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
     "testsrc2=size=320x240:rate=10:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
     os.path.join(slot, "clip.mp4")], check=True, timeout=120, stdin=subprocess.DEVNULL)
processor = subprocess.Popen(
    [sys.executable, "-m", "aegis_media_processor", "--scratch", scratch, "--roots", roots],
    env={**os.environ, "AEGIS_MEDIA_PROCESSOR_CONCURRENCY": "2"}, stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL)
deadline = time.monotonic() + 30
while not os.path.isdir(os.path.join(scratch, "done")):
    assert processor.poll() is None and time.monotonic() < deadline, "processor did not start"
    time.sleep(0.1)
# The media worker's default stage limits (aegis_apps.media.config): wall seconds, MiB.
# A backfill job (probe plus thumbnail or poster) gets the probe's wall plus its rendition's.
stage = {"probe": (30, 1024), "thumbnail": (60, 1536), "poster": (60, 1536),
         "preview": (120, 1536), "transcode": (600, 3072), "backfill": (90, 1536)}
jobs = []
for name, op in ((b"photo.jpg", "probe"), (b"photo.jpg", "thumbnail"), (b"photo.jpg", "preview"),
                 (b"photo.jpg", "backfill"), (b"clip.mp4", "probe"), (b"clip.mp4", "poster"),
                 (b"clip.mp4", "transcode"), (b"clip.mp4", "backfill")):
    observed = os.stat(os.path.join(slot.encode(), name))
    kind = p.media_kind_for_path(name)
    job = p.Job(uuid.uuid4().hex, op, kind, "background",
                p.SourceInput("synthetic", name, observed.st_dev, observed.st_ino,
                              observed.st_size, observed.st_mtime_ns),
                p.default_params(op, kind),
                p.Limits(wall_seconds=stage[op][0], cpu_seconds=stage[op][0],
                         memory_bytes=stage[op][1] << 20, output_bytes=256 << 20))
    p.write_job(os.path.join(scratch, "inbox"), job)
    jobs.append(job)
summary = {}
deadline = time.monotonic() + 600
for job in jobs:
    directory = os.path.join(scratch, "done", job.id)
    while not os.path.exists(os.path.join(directory, "result.json")):
        assert time.monotonic() < deadline, "the processor did not finish its jobs"
        time.sleep(0.2)
    result = p.decode_result(
        p.read_regular_file(os.path.join(directory, "result.json"), p.MAX_RESULT_BYTES), job)
    metadata = result.metadata
    summary[f"{job.op}:{job.kind}"] = {
        "status": result.status, "code": result.safe_error_code,
        "files": sorted(os.listdir(directory)),
        "outputs": [[o.name, o.width, o.height] for o in result.outputs],
        "size": None if metadata is None else [metadata.width, metadata.height],
        "codec": None if metadata is None else metadata.video_codec,
    }
processor.send_signal(signal.SIGTERM)
summary["exit"] = processor.wait(timeout=30)
summary["uid"] = os.getuid()
summary["left"] = sorted(os.listdir(os.path.join(scratch, "work")))
print(json.dumps(summary, sort_keys=True))
'''

IMAGE_FACTS = r'''
import json, os, shutil, subprocess, sys
names = json.loads(sys.argv[1])
def tool(*arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True,
                          timeout=60).stdout
scratch = os.stat("/srv/aegis/scratch")
inventory = os.stat("/usr/share/aegis/media-tools.json")
print(json.dumps({
    "clients": {name: shutil.which(name) for name in names},
    "apt_lists": sorted(os.listdir("/var/lib/apt/lists")),
    "ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe"),
    "version": tool("ffmpeg", "-hide_banner", "-version").splitlines()[0],
    "probe_version": tool("ffprobe", "-hide_banner", "-version").splitlines()[0],
    "encoders": tool("ffmpeg", "-hide_banner", "-encoders"),
    "decoders": tool("ffmpeg", "-hide_banner", "-decoders"),
    "package": tool("dpkg-query", "--show", "--showformat=${Version}", "ffmpeg"),
    "inventory": json.load(open("/usr/share/aegis/media-tools.json", encoding="utf-8")),
    "inventory_stat": [inventory.st_uid, oct(inventory.st_mode & 0o7777)],
    "scratch": [scratch.st_uid, scratch.st_gid, oct(scratch.st_mode & 0o7777)],
    "uid": os.getuid(), "gid": os.getgid(),
    "pillow_heif": __import__("pillow_heif").__version__,
    "processor": __import__("aegis_media_processor.protocol").protocol.PROCESSOR_VERSION,
}))
'''


def _inspect_image(tag: str) -> dict[str, Any] | None:
    inspected = run_container([*CONTAINER_COMMAND, "image", "inspect", tag],
                              capture_output=True, text=True, timeout=60, check=False)
    if inspected.returncode != 0:
        return None
    payload = json.loads(inspected.stdout)
    assert isinstance(payload, list) and len(payload) == 1
    info: dict[str, Any] = payload[0]
    return info


def _build(tag: str, target: str | None) -> None:
    command = [*CONTAINER_COMMAND, "build", "--label", f"{OWNER_LABEL}={tag}", "--tag", tag,
               "--file", DOCKERFILE]
    if target is not None:
        command += ["--target", target]
    built = run_container([*command, "."], cwd=REPOSITORY, capture_output=True, text=True,
                          timeout=BUILD_TIMEOUT, check=False)
    assert built.returncode == 0, built.stderr[-4000:]


def _remove_image(tag: str) -> None:
    info = _inspect_image(tag)
    if info is None:
        return
    labels = (info.get("Config") or {}).get("Labels") or {}
    assert labels.get(OWNER_LABEL) == tag, "refusing to remove an image this run did not build"
    run_container([*CONTAINER_COMMAND, "image", "rm", "--no-prune", tag], check=True,
                  capture_output=True, timeout=120)


@pytest.fixture(scope="module")
def media_images() -> Iterator[dict[str, str]]:
    run = uuid.uuid4().hex[:12]
    tags = {"media": f"aegis-media-verify-{run}", "test": f"aegis-media-test-verify-{run}"}
    try:
        _build(tags["media"], None)
        _build(tags["test"], "media-test")
        yield tags
    finally:
        for tag in tags.values():
            _remove_image(tag)


def run_owned(
    image: str, entrypoint: str, arguments: Sequence[str], *,
    volumes: Sequence[str] = (), timeout: int = 300,
) -> tuple[int, str, str]:
    """One disposable container; the run-unique name is removed by exact identity afterwards."""
    name = f"aegis-media-check-{uuid.uuid4().hex}"
    command = [
        *CONTAINER_COMMAND, "run", "--rm", "--name", name, "--label", f"{OWNER_LABEL}={name}",
        *CONTAINMENT, *(item for volume in volumes for item in ("--volume", volume)),
        "--entrypoint", entrypoint, image, *arguments,
    ]
    try:
        result = run_container(command, capture_output=True, text=True, timeout=timeout,
                               check=False)
        return result.returncode, result.stdout, result.stderr
    finally:
        inspection = run_container([*CONTAINER_COMMAND, "container", "inspect", name],
                                   capture_output=True, text=True, timeout=30, check=False)
        if inspection.returncode == 0:
            owned = json.loads(inspection.stdout)[0]
            assert owned["Config"]["Labels"][OWNER_LABEL] == name
            run_container([*CONTAINER_COMMAND, "rm", "--force", owned["Id"]],
                          capture_output=True, text=True, timeout=30, check=True)


def test_media_dockerfile_reuses_the_locked_backend_bases_and_ends_unprivileged() -> None:
    """Static: no build. The same digest-pinned bases as the backend image, ffmpeg pinned."""
    dockerfile = (REPOSITORY / DOCKERFILE).read_text(encoding="utf-8")
    backend = (REPOSITORY / "docker/backend.Dockerfile").read_text(encoding="utf-8")
    lock = {
        line.split()[0]: line.split()[1:]
        for line in (REPOSITORY / "deploy/images.lock").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    python = f"docker.io/library/python:{lock['python'][0]}@{lock['python'][1]}"
    uv = f"ghcr.io/astral-sh/uv:{lock['uv'][0]}@{lock['uv'][1]}"
    bases = re.findall(r"^FROM (\S+)", dockerfile, flags=re.MULTILINE)
    stages = set(re.findall(r"^FROM \S+ AS (\S+)$", dockerfile, flags=re.MULTILINE))

    assert set(bases) == {python, uv, "build", "runtime"}
    assert {base for base in bases if "@" in base} <= set(
        re.findall(r"^FROM (\S+)", backend, flags=re.MULTILINE)
    )
    assert stages == {"uv", "build", "test-environment", "runtime", "media-test"}
    assert "COPY backend/aegis_media_processor ./backend/aegis_media_processor" in dockerfile
    assert "COPY backend/aegis_media_processor ./backend/aegis_media_processor" in backend
    # Unpinned on purpose: debian-security keeps only the latest version (see the inventory).
    assert "apt-get install --yes --no-install-recommends ffmpeg \\\n" in dockerfile
    assert "ffmpeg=" not in dockerfile and "AEGIS_FFMPEG_VERSION" not in dockerfile
    assert "rm -rf /var/lib/apt/lists/*" in dockerfile
    final = dockerfile.rsplit("\nFROM runtime\n", 1)[1]
    assert 'LABEL org.aegis.ffmpeg.version="see /usr/share/aegis/media-tools.json"' in final
    assert "COPY --from=build /app/.venv /app/.venv" in final
    assert (
        "python -I -B -m aegis_media_processor.inventory /usr/share/aegis/media-tools.json"
    ) in final
    assert final.index("aegis_media_processor.inventory") < final.index("USER 10001:10001")
    assert "test-environment" not in final
    assert re.search(r"^USER 10001:10001$", final, flags=re.MULTILINE)
    assert 'ENTRYPOINT ["python", "-m", "aegis_media_processor"]' in final
    assert final.rstrip().endswith("CMD []")


def test_media_image_runs_the_processor_as_the_unprivileged_user(
    media_images: dict[str, str],
) -> None:
    info = _inspect_image(media_images["media"])

    assert info is not None
    config = info["Config"]
    assert config["User"] == "10001:10001"
    assert config["Entrypoint"] == ["python", "-m", "aegis_media_processor"]
    assert not config.get("Cmd")
    assert config["WorkingDir"] == "/app/backend"
    assert config["Labels"]["org.aegis.ffmpeg.version"] == (
        "see /usr/share/aegis/media-tools.json"
    )


def _locked_version(name: str) -> str:
    lock = tomllib.loads((REPOSITORY / "uv.lock").read_text(encoding="utf-8"))
    (version,) = [package["version"] for package in lock["package"] if package["name"] == name]
    return str(version)


def test_media_image_records_its_decoding_stack_and_has_no_network_clients(
    media_images: dict[str, str],
) -> None:
    code, stdout, stderr = run_owned(
        media_images["media"], "python", ["-I", "-c", IMAGE_FACTS, json.dumps(NETWORK_CLIENTS)],
    )

    assert code == 0, stderr[-4000:]
    facts = json.loads(stdout)
    inventory = facts["inventory"]
    assert facts["inventory_stat"] == [0, "0o444"]
    assert inventory["ffmpeg"]["path"] == "/usr/bin/ffmpeg"
    assert inventory["ffmpeg"]["version"] == facts["package"]
    assert inventory["ffprobe"]["path"] == "/usr/bin/ffprobe"
    assert inventory["ffprobe"]["version"] == inventory["ffmpeg"]["version"]
    upstream = inventory["ffmpeg"]["version"].split(":", 1)[-1]
    assert upstream.split(".", 1)[0] == "7", inventory["ffmpeg"]
    assert inventory["ffmpeg_banner"] == facts["version"]
    assert {"h264", "hevc", *LEGACY_DECODERS} <= set(inventory["ffmpeg_decoders"])
    assert {"libx264", "aac", "png"} <= set(inventory["ffmpeg_encoders"])
    assert "libde265" in inventory["heif_decoders"]
    assert inventory["libheif"]
    assert inventory["pillow"] == _locked_version("pillow")
    assert inventory["pillow_heif"] == _locked_version("pillow-heif") == facts["pillow_heif"]
    assert inventory["pillow_avif"] is True and inventory["pillow_webp"] is True
    assert facts["ffmpeg"] == "/usr/bin/ffmpeg" and facts["ffprobe"] == "/usr/bin/ffprobe"
    assert facts["version"].startswith("ffmpeg version ")
    assert facts["probe_version"].startswith("ffprobe version ")
    for encoder in ("libx264", "aac", "png"):
        assert re.search(rf"^\s*\S+\s+{encoder}\s", facts["encoders"], re.MULTILINE), encoder
    for decoder in ("h264", "hevc", "vp9", "aac", "mjpeg", *LEGACY_DECODERS):
        assert re.search(rf"^\s*\S+\s+{decoder}\s", facts["decoders"], re.MULTILINE), decoder
    assert facts["clients"] == dict.fromkeys(NETWORK_CLIENTS)
    assert facts["apt_lists"] == []
    assert facts["scratch"] == [10001, 10001, "0o770"]
    assert (facts["uid"], facts["gid"]) == (10001, 10001)
    assert facts["pillow_heif"] and facts["processor"]


def test_media_processor_serves_jobs_offline_in_the_image(media_images: dict[str, str]) -> None:
    code, stdout, stderr = run_owned(
        media_images["media"], "python", ["-I", "-c", PROCESSOR_RUN], timeout=900,
    )

    assert code == 0, stderr[-4000:]
    summary = json.loads(stdout.strip().splitlines()[-1])
    assert summary.pop("exit") == 0
    assert summary.pop("uid") == 10001
    assert summary.pop("left") == []
    assert all(job["status"] == "ok" and job["code"] is None for job in summary.values()), summary
    assert summary["probe:photo"]["size"] == [1200, 800]
    assert summary["probe:video"]["size"] == [320, 240]
    assert summary["probe:video"]["codec"] == "h264"
    assert summary["thumbnail:photo"]["outputs"][0][:2] == ["thumbnail", 400]
    assert summary["preview:photo"]["outputs"] == [["preview", 1200, 800]]
    assert summary["poster:video"]["outputs"] == [["thumbnail", 320, 240], ["poster", 320, 240]]
    assert summary["transcode:video"]["outputs"] == [["transcode", 320, 240]]
    assert summary["poster:video"]["files"] == ["poster.webp", "result.json", "thumbnail.webp"]
    assert summary["transcode:video"]["files"] == ["result.json", "transcode.mp4"]
    assert summary["probe:photo"]["files"] == ["result.json"]
    # One backfill job per asset: the probe's metadata and the thumbnail or poster pair.
    assert summary["backfill:photo"]["size"] == [1200, 800]
    assert summary["backfill:photo"]["outputs"] == summary["thumbnail:photo"]["outputs"]
    assert summary["backfill:photo"]["files"] == ["result.json", "thumbnail.webp"]
    assert summary["backfill:video"]["size"] == [320, 240]
    assert summary["backfill:video"]["codec"] == "h264"
    assert summary["backfill:video"]["outputs"] == summary["poster:video"]["outputs"]
    assert summary["backfill:video"]["files"] == summary["poster:video"]["files"]


def _mounted_processor_tests(tmp_path: Path) -> Path:
    """A read-only copy of the processor tests (without the conftest) for the media-test image."""
    staging = tmp_path / "staging"
    package = staging / "media_processor"
    package.mkdir(parents=True)
    package.chmod(0o755)
    sources = REPOSITORY / "backend" / "tests" / "unit" / "media_processor"
    for source in sorted(sources.glob("*.py")):
        if source.name == "conftest.py":
            continue
        shutil.copyfile(source, package / source.name)
        (package / source.name).chmod(0o644)
    return copy_bind_inputs(tmp_path, {"media_processor": package})["media_processor"]


def _pytest_in_image(image: str, mounted: Path, selection: Sequence[str]) -> int:
    code, stdout, stderr = run_owned(
        image, "python",
        ["-m", "pytest", "-p", "no:cacheprovider", "-p", "no:django", "--noconftest",
         "-c", "/dev/null", "--rootdir", "/src", "-q", "-rA", *selection],
        volumes=[f"{mounted}:/src/media_processor:ro"], timeout=1200,
    )
    assert code == 0, (stdout[-6000:], stderr[-2000:])
    passed = re.search(r"\b(\d+) passed\b", stdout)
    assert passed, stdout[-2000:]
    return int(passed[1])


def test_video_operations_pass_inside_the_media_image(
    media_images: dict[str, str], tmp_path: Path,
) -> None:
    """Runs the ``media_tools`` tests where ffmpeg exists, selected explicitly by marker."""
    mounted = _mounted_processor_tests(tmp_path)

    passed = _pytest_in_image(media_images["test"], mounted, [
        "-m", "media_tools", "/src/media_processor/test_video_ops.py",
    ])

    # 14 before the legacy formats, plus their 12 clips and 2 loop and refusal tests.
    assert passed >= 28


def test_landlock_confines_the_child_inside_the_media_image(
    media_images: dict[str, str], tmp_path: Path,
) -> None:
    """The Landlock and seccomp denials, under the container's own seccomp profile and user."""
    mounted = _mounted_processor_tests(tmp_path)

    passed = _pytest_in_image(media_images["test"], mounted, [
        "/src/media_processor/test_landlock.py",
    ])

    assert passed >= 14
