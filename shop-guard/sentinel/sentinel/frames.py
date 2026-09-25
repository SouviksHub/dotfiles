"""Samples evenly spaced JPEG frames from a clip with ffmpeg."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _duration(path: Path) -> float:
    # `ffmpeg -i` with no output exits non-zero but prints the container header.
    err = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = _DURATION.search(err)
    if not m:
        raise RuntimeError("could not read clip duration")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def sample_frames(clip: bytes, count: int = 10, width: int = 1024) -> list[tuple[float, bytes]]:
    """Returns [(seconds_from_clip_start, jpeg_bytes)]."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "clip.mp4"
        src.write_bytes(clip)
        duration = _duration(src)
        frames: list[tuple[float, bytes]] = []
        for i in range(count):
            t = duration * (i + 0.5) / count
            dst = Path(tmp) / f"f{i:02d}.jpg"
            subprocess.run(
                [FFMPEG, "-v", "error", "-ss", f"{t:.2f}", "-i", str(src),
                 "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", "-y", str(dst)],
                check=True,
            )
            if dst.exists() and dst.stat().st_size:
                frames.append((round(t, 1), dst.read_bytes()))
        return frames
