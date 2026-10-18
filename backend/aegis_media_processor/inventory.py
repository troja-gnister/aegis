"""The decoding stack an aegis-media image contains, recorded when the image is built.

``python -I -m aegis_media_processor.inventory /usr/share/aegis/media-tools.json`` writes:

- the Debian package and version that own ``ffmpeg`` and ``ffprobe`` (``dpkg-query``). Debian's
  ffmpeg is installed unpinned, because debian-security keeps only the latest version, so the
  image records the version it actually got;
- which of the ffmpeg decoders and encoders the processor relies on are present;
- the Pillow, pillow-heif and libheif versions, and libheif's decoders.

It runs at build time only; the processor never imports it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from typing import Final

INVENTORY_PATH: Final = "/usr/share/aegis/media-tools.json"
FFMPEG_DECODERS: Final = (
    "h264", "hevc", "vp8", "vp9", "av1", "mpeg4", "aac", "mp3", "opus",
    # The legacy formats: 3GP/3G2, WMV/ASF, FLV, MPG/VOB, OGV and DivX.
    "h263", "msmpeg4", "wmv1", "wmv2", "wmv3", "vc1", "flv", "vp6f", "mpeg1video", "mpeg2video",
    "theora", "amrnb", "amrwb", "wmav1", "wmav2", "wmapro", "mp2", "ac3", "vorbis",
)
FFMPEG_ENCODERS: Final = ("libx264", "aac", "png")
_SAFE_ENVIRONMENT: Final = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C.UTF-8"}
_CODEC_LINE: Final = re.compile(r"\s*[A-Z.]{6}\s+(\S+)\s")


def _output(*argv: str) -> str:
    return subprocess.run(
        list(argv), check=True, capture_output=True, text=True, timeout=60,
        stdin=subprocess.DEVNULL, env=_SAFE_ENVIRONMENT,
    ).stdout


def debian_package(path: str) -> dict[str, str]:
    """The installed package owning ``path`` and its exact version."""
    owner = None
    for line in _output("dpkg-query", "--search", path).splitlines():
        package, separator, owned = line.rpartition(": ")
        if separator and owned.strip() == path:
            owner = package.split(",")[0].strip()
            break
    if not owner:
        raise RuntimeError("no installed package owns the tool")
    version = _output("dpkg-query", "--show", "--showformat=${Version}", owner).strip()
    return {"path": path, "package": owner, "version": version}


def ffmpeg_codecs(listing: str, wanted: Sequence[str]) -> list[str]:
    """The wanted names present in ``ffmpeg -decoders`` or ``-encoders`` output."""
    present = {match[1] for match in map(_CODEC_LINE.match, listing.splitlines()) if match}
    return [name for name in wanted if name in present]


def collect() -> dict[str, object]:
    import PIL
    import pillow_heif
    from PIL import features

    heif = pillow_heif.libheif_info()
    decoders = heif.get("decoders") or {}
    return {
        "ffmpeg": debian_package("/usr/bin/ffmpeg"),
        "ffprobe": debian_package("/usr/bin/ffprobe"),
        "ffmpeg_banner": _output("ffmpeg", "-hide_banner", "-version").splitlines()[0],
        "ffmpeg_decoders": ffmpeg_codecs(
            _output("ffmpeg", "-hide_banner", "-decoders"), FFMPEG_DECODERS,
        ),
        "ffmpeg_encoders": ffmpeg_codecs(
            _output("ffmpeg", "-hide_banner", "-encoders"), FFMPEG_ENCODERS,
        ),
        "pillow": PIL.__version__,
        "pillow_avif": bool(features.check("avif")),
        "pillow_webp": bool(features.check("webp")),
        "pillow_heif": pillow_heif.__version__,
        "libheif": str(heif.get("libheif") or ""),
        "heif_decoders": sorted(str(name) for name in decoders),
    }


def write(path: str) -> None:
    data = json.dumps(collect(), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
    with open(descriptor, "wb") as handle:
        handle.write(data)


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1 or not os.path.isabs(argv[0]):
        print("usage: python -m aegis_media_processor.inventory <absolute path>", file=sys.stderr)
        return 64
    write(argv[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
