"""Media backfill throughput: synthetic photos through the real processor, before and after.

``uv run --locked python -m scripts.benchmarks.media_backfill [--photos N] [--image TAG]``

A local measurement, not a gate. It builds the aegis-media image from the working tree under a
run-unique tag and owner label (or uses ``--image``), then runs one disposable, offline,
read-only container per scenario. Inside it, a driver generates synthetic 12-megapixel JPEGs
(EXIF orientation and capture time included), starts the real processor loop (Landlock,
seccomp, rlimits, nice 10) and emulates the media worker's cadence: it keeps a bounded number
of jobs queued, collects ``done/`` on its tick and queues the next ones.

- ``before``: concurrency 2 and 2 CPUs, a 0.5 s processor poll, 2 jobs in flight on a 0.25 s
  tick, and two jobs per photo (every probe first, then each thumbnail queued behind them);
- ``after``: concurrency 4 and 4 CPUs, a 0.1 s poll, 8 jobs in flight on a 0.05 s tick, and one
  ``backfill`` job per photo.

It reports jobs per second, thumbnails per second, the processor's CPU seconds per photo
(the loop and its reaped children) and its idle CPU share afterwards. The worker's database
work is not included, and the photos are read from page cache, so real rates are lower.
Owned images are removed with ``--no-prune``; nothing else is touched.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from aegisctl.container_engine import container_command, sanitized_environment
from aegisctl.container_launch import controlled_container_argv

REPOSITORY = Path(__file__).resolve().parents[2]
OWNER_LABEL = "aegis.verify.owner"
SCENARIOS: dict[str, dict[str, Any]] = {
    "before": {"concurrency": 2, "cpus": "2", "memory": "7g", "poll": 0.5, "in_flight": 2,
               "tick": 0.25, "backfill": False},
    "after": {"concurrency": 4, "cpus": "4", "memory": "13g", "poll": 0.1, "in_flight": 8,
              "tick": 0.05, "backfill": True},
}

DRIVER = r'''
import json, os, shutil, signal, subprocess, sys, time, uuid
from PIL import Image, ImageFilter
from aegis_media_processor import protocol as p

config = json.loads(sys.argv[1])
roots, scratch = "/tmp/roots", "/tmp/scratch"
slot = os.path.join(roots, "synthetic")
os.makedirs(os.path.join(slot, "album"))
os.makedirs(scratch)
# A few distinct phone-sized photos, hard-linked so every photo is its own job and path.
sources = []
for index in range(4):
    width, height = 4000, 3000
    noise = Image.effect_noise((width // 8, height // 8), 64).resize((width, height))
    base = Image.merge("RGB", (
        Image.linear_gradient("L").resize((width, height)),
        noise,
        Image.radial_gradient("L").resize((width, height)).filter(ImageFilter.GaussianBlur(4)),
    ))
    exif = Image.Exif()
    exif[0x0112] = 6
    exif.get_ifd(0x8769)[0x9003] = "2024:05:%02d 10:20:30" % (index + 1)
    path = os.path.join(slot, "source-%d.jpg" % index)
    base.save(path, quality=90, exif=exif.tobytes())
    sources.append(path)
names = []
for index in range(config["photos"]):
    name = b"album/IMG_%05d.JPG" % index
    os.link(os.fsencode(sources[index % len(sources)]), os.path.join(slot.encode(), name))
    names.append(name)

LIMITS = {  # the media worker's defaults: wall seconds, memory MiB, output bytes
    "probe": (30, 1024, 64 << 10), "thumbnail": (60, 1536, 8 << 20),
    "backfill": (90, 1536, 8 << 20),
}

def job(name, op):
    observed = os.stat(os.path.join(slot.encode(), name))
    wall, memory, output = LIMITS[op]
    return p.Job(uuid.uuid4().hex, op, "photo", "background",
                 p.SourceInput("synthetic", name, observed.st_dev, observed.st_ino,
                               observed.st_size, observed.st_mtime_ns),
                 p.default_params(op, "photo"),
                 p.Limits(wall_seconds=wall, cpu_seconds=2 * wall,
                          memory_bytes=memory << 20, output_bytes=output))

def cpu_seconds(pid):
    fields = open("/proc/%d/stat" % pid, "rb").read().rsplit(b")", 1)[1].split()
    ticks = os.sysconf("SC_CLK_TCK")
    return sum(int(value) for value in fields[11:15]) / ticks  # utime stime cutime cstime

runner = (
    "import signal, sys\n"
    "from aegis_media_processor.loop import Processor\n"
    "processor = Processor(sys.argv[1], sys.argv[2], int(sys.argv[3]),"
    " poll_seconds=float(sys.argv[4]))\n"
    "signal.signal(signal.SIGTERM, lambda *_: processor.request_stop())\n"
    "processor.run()\n"
)
processor = subprocess.Popen(
    [sys.executable, "-I", "-c", runner, scratch, roots, str(config["concurrency"]),
     str(config["poll"])], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL)
deadline = time.monotonic() + 30
while not os.path.isdir(os.path.join(scratch, "done")):
    assert processor.poll() is None and time.monotonic() < deadline, "processor did not start"
    time.sleep(0.05)
inbox, done = os.path.join(scratch, "inbox"), os.path.join(scratch, "done")
queue = [job(name, "backfill" if config["backfill"] else "probe") for name in names]
in_flight, finished, failed, thumbnails = {}, 0, 0, 0
started = time.monotonic()
cpu_before = cpu_seconds(processor.pid)
while queue or in_flight:
    for entry in os.listdir(done):
        current = in_flight.pop(entry, None)
        if current is None:
            continue
        result = p.decode_result(p.read_regular_file(
            os.path.join(done, entry, "result.json"), p.MAX_RESULT_BYTES), current)
        shutil.rmtree(os.path.join(done, entry))
        finished += 1
        if result.status != "ok":
            failed += 1
        elif current.op == "probe":
            queue.append(job(current.input.path, "thumbnail"))  # re-queued at the tail
        else:
            thumbnails += 1
    while queue and len(in_flight) < config["in_flight"]:
        submitted = queue.pop(0)
        p.write_job(inbox, submitted)
        in_flight[submitted.id] = submitted
    if queue or in_flight:
        time.sleep(config["tick"])
elapsed = time.monotonic() - started
busy_cpu = cpu_seconds(processor.pid) - cpu_before
idle_start = cpu_seconds(processor.pid)
time.sleep(10)
idle_cpu = cpu_seconds(processor.pid) - idle_start
processor.send_signal(signal.SIGTERM)
processor.wait(timeout=30)
print(json.dumps({
    "photos": config["photos"], "jobs": finished, "failed": failed, "seconds": round(elapsed, 2),
    "jobs_per_second": round(finished / elapsed, 2),
    "thumbnails_per_second": round(thumbnails / elapsed, 2),
    "processor_cpu_seconds_per_photo": round(busy_cpu / config["photos"], 3),
    "processor_cpu_share": round(busy_cpu / elapsed, 2),
    "idle_cpu_percent": round(100 * idle_cpu / 10, 2),
}))
'''


def _run(command: Sequence[str], *, timeout: int, check: bool = True,
         cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = sanitized_environment(os.environ)
    argv = controlled_container_argv(
        list(command), environment=environment, canonical_base=REPOSITORY / "compose.yaml",
        working_directory=cwd,
    )
    completed = subprocess.run(argv, env=environment, cwd=cwd, capture_output=True, text=True,
                               timeout=timeout, check=False)
    if check and completed.returncode != 0:
        raise SystemExit(f"container command failed: {completed.stderr[-2000:]}")
    return completed


def _owned_image_id(tag: str) -> str | None:
    """The image ID behind ``tag`` if this run built it (its owner label is the tag)."""
    inspected = _run([*container_command(), "image", "inspect", tag], timeout=60, check=False)
    if inspected.returncode != 0:
        return None
    info = json.loads(inspected.stdout)[0]
    labels = (info.get("Config") or {}).get("Labels") or {}
    identity = info.get("Id")
    if labels.get(OWNER_LABEL) != tag or not isinstance(identity, str) or not identity:
        return None
    return identity


def _scenario(image: str, name: str, photos: int) -> dict[str, Any]:
    settings = SCENARIOS[name]
    container = f"aegis-media-bench-{uuid.uuid4().hex[:12]}"
    config = {key: settings[key] for key in ("concurrency", "poll", "in_flight", "tick",
                                             "backfill")} | {"photos": photos}
    command = [
        *container_command(), "run", "--rm", "--name", container,
        "--label", f"{OWNER_LABEL}={container}", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--pids-limit", "256", "--memory", settings["memory"], "--cpus", settings["cpus"],
        "--tmpfs", "/tmp:size=512m,mode=1777", "--entrypoint", "python", image,
        "-I", "-c", DRIVER, json.dumps(config),
    ]
    try:
        completed = _run(command, timeout=3600)
    finally:
        inspected = _run([*container_command(), "container", "inspect", container],
                         timeout=30, check=False)
        if inspected.returncode == 0:
            owned = json.loads(inspected.stdout)[0]
            if owned["Config"]["Labels"].get(OWNER_LABEL) == container:
                _run([*container_command(), "rm", "--force", owned["Id"]], timeout=60)
    report: dict[str, Any] = json.loads(completed.stdout.strip().splitlines()[-1])
    return {"scenario": name, **{key: settings[key] for key in ("concurrency", "cpus")},
            **report}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.benchmarks.media_backfill")
    parser.add_argument("--photos", type=int, default=120)
    parser.add_argument("--image", help="an aegis-media image to use instead of building one")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), action="append")
    arguments = parser.parse_args(argv)
    if not 8 <= arguments.photos <= 5_000:
        parser.error("--photos must be from 8 to 5000")
    image = arguments.image
    built = None
    if image is None:
        built = image = f"aegis-media-bench-{uuid.uuid4().hex[:12]}"
        _run([*container_command(), "build", "--label", f"{OWNER_LABEL}={built}", "--tag",
              built, "--file", "docker/media.Dockerfile", "."], timeout=3600, cwd=REPOSITORY)
    try:
        reports = [
            _scenario(image, name, arguments.photos)
            for name in (arguments.scenario or ["before", "after"])
        ]
    finally:
        identity = None if built is None else _owned_image_id(built)
        if identity is not None:
            # By its ID, so a re-pointed tag can never remove another image.
            _run([*container_command(), "image", "rm", "--no-prune", identity], timeout=120)
    for report in reports:
        print(json.dumps(report, sort_keys=True))
    if len(reports) == 2:
        before, after = reports
        print(json.dumps({
            "thumbnails_per_second_speedup": round(
                after["thumbnails_per_second"] / before["thumbnails_per_second"], 2,
            ),
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
