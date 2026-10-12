"""One job's decoding child: ``python -I -B -m aegis_media_processor.child <op> <job dir> <fd>``.

The processor loop starts this under the job's rlimits, at low priority, with a scrubbed
environment and a Landlock ruleset (``landlock.py``). It inherits exactly one descriptor, ``<fd>``:
the original, which the loop opened by validated components and fingerprint-checked. The child
never resolves a root path, and Landlock would refuse it anyway.

It reads ``<job dir>/job.json`` (the loop's file; readable, not writable, for the child) and
validates it again, checks that the descriptor is still the fingerprinted file, then decodes it
in this process (Pillow and pillow-heif) or through its own sandboxed ffmpeg and ffprobe, which
inherit the ruleset. A ``backfill`` job probes and renders in this one child, from the one
descriptor. Outputs and temporary files go only into ``<job dir>/out/``. The result
document is the only thing written to stdout; exit status 0 means one was written. After
decoding, the descriptor's size and mtime are checked again, so a file edited during decoding
reports ``changed``.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Sequence
from typing import Final, cast

from .ops import OperationError
from .protocol import (
    JOB_FILE,
    MAX_JOB_BYTES,
    OP_OUTPUTS,
    OPS,
    ImageParams,
    Job,
    Output,
    PosterParams,
    ProtocolError,
    Result,
    Status,
    TranscodeParams,
    decode_job,
    encode_result,
    job_outputs,
    read_regular_file,
)
from .sandbox import output_limit_error
from .source import InputRefused, SourceChanged, SourceUnavailable, matches

OUTPUT_DIR: Final = "out"
EXIT_USAGE: Final = 64
EXIT_INVALID_JOB: Final = 65
# The child reports its own timeout a little before the loop's deadline kills it.
DEADLINE_MARGIN_SECONDS: Final = 1.0


def failure(job: Job, status: Status, code: str) -> Result:
    return Result(id=job.id, status=status, safe_error_code=code, metadata=None, outputs=())


def open_failure(job: Job, error: BaseException) -> Result:
    """The result for a job whose input the loop could not open."""
    if isinstance(error, SourceChanged):
        return failure(job, "changed", "source_changed")
    if isinstance(error, InputRefused):
        return failure(job, "failed", "invalid_job")
    if isinstance(error, SourceUnavailable):
        return failure(job, "failed", "source_unavailable")
    raise TypeError("not an input opening error")


def _operate(job: Job, descriptor: int, directory: str, deadline: float) -> Result:
    # Imported here: the processor loop imports this module's names but never Pillow.
    from .ops import image, probe, video

    image.configure()
    outputs: tuple[Output, ...]
    if job.op == "probe":
        metadata = (
            probe.probe_photo(descriptor) if job.kind == "photo"
            else probe.probe_video(descriptor, directory, job.limits, deadline)
        )
        return Result(id=job.id, status="ok", safe_error_code=None, metadata=metadata, outputs=())
    if job.op == "backfill":
        if job.kind == "photo":
            (spec,) = job_outputs(job.op, job.kind)
            metadata, rendered = probe.backfill_photo(
                descriptor, directory, spec, cast(ImageParams, job.params),
            )
            outputs = (rendered,)
        else:
            metadata, outputs = probe.backfill_video(
                descriptor, directory, cast(PosterParams, job.params), job.limits, deadline,
            )
        return Result(id=job.id, status="ok", safe_error_code=None, metadata=metadata,
                      outputs=outputs)
    if job.op in ("thumbnail", "preview"):
        (spec,) = OP_OUTPUTS[job.op]
        outputs = (
            image.render_photo(descriptor, directory, spec, cast(ImageParams, job.params)),
        )
    elif job.op == "poster":
        outputs = video.make_poster(
            descriptor, directory, cast(PosterParams, job.params), job.limits, deadline,
        )
    else:
        outputs = (video.transcode(
            descriptor, directory, cast(TranscodeParams, job.params), job.limits, deadline,
        ),)
    return Result(id=job.id, status="ok", safe_error_code=None, metadata=None, outputs=outputs)


def execute(job: Job, descriptor: int, directory: str) -> Result:
    """Decode one validated job from its input descriptor into ``directory``.

    Every outcome, including hostile input, becomes a result.
    """
    deadline = time.monotonic() + max(job.limits.wall_seconds - DEADLINE_MARGIN_SECONDS, 0.5)
    if not matches(descriptor, job.input):
        return failure(job, "changed", "source_changed")
    try:
        result = _operate(job, descriptor, directory, deadline)
    except OperationError as error:
        return failure(job, error.status, error.code)
    except MemoryError:
        return failure(job, "failed", "memory_limit")
    except Exception as error:  # hostile input may raise anything from any decoder
        return failure(job, "failed", "output_limit" if output_limit_error(error) else
                       "decode_failed")
    if not matches(descriptor, job.input):
        return failure(job, "changed", "source_changed")
    return result


def main(argv: Sequence[str]) -> int:
    if len(argv) != 3 or argv[0] not in OPS:
        return EXIT_USAGE
    op, directory, number = argv
    if (not os.path.isabs(directory) or "\0" in directory or not number.isascii()
            or not number.isdecimal() or len(number) > 7 or int(number) < 3):
        return EXIT_USAGE
    try:
        job = decode_job(read_regular_file(os.path.join(directory, JOB_FILE), MAX_JOB_BYTES))
    except (ProtocolError, OSError):
        return EXIT_INVALID_JOB
    if job.op != op or os.path.basename(os.path.normpath(directory)) != job.id:
        return EXIT_INVALID_JOB
    result = execute(job, int(number), os.path.join(directory, OUTPUT_DIR))
    try:
        document = encode_result(result, job)
    except ProtocolError:
        document = encode_result(failure(job, "failed", "invalid_result"), job)
    sys.stdout.buffer.write(document)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
