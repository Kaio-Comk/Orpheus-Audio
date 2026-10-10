"""Fix AC-3 / E-AC-3 / DTS audio that browsers and Chromecast cannot play.

The video stream is copied untouched (no quality loss, minutes instead of hours);
only the first audio track becomes AAC 128 kbps. The result is validated with
ffprobe before it is promoted, and the source file is never modified.

Everything is async and uses `asyncio.create_subprocess_exec`: a blocking
`subprocess.run(ffmpeg)` inside a coroutine freezes the event loop for the whole
encode (it once cost us a Telegram connection in the middle of a 700 MB upload).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Audio codecs that browsers / Chromecast play natively.
PLAYABLE_AUDIO = {"aac", "mp3", "opus", "vorbis"}

ProgressCb = Callable[[int, int], None]     # (percent 0-99, estimated seconds left)


class AudioRepairError(RuntimeError):
    """The repair failed; the source file is untouched and no temp file is left."""


def _bin(name: str, override: Optional[str]) -> str:
    path = override or shutil.which(name)
    if not path:
        raise AudioRepairError(f"{name} not found; install ffmpeg or pass its path")
    return path


async def _run_capture(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await proc.communicate()
    except asyncio.CancelledError:
        _kill(proc)
        raise
    return out.decode("utf-8", errors="ignore")


async def probe_codec(path: Path, stream: str = "a:0", *, ffprobe: Optional[str] = None) -> str:
    """codec_name of the FIRST stream of that type ('a:0' or 'v:0'); '' if absent.

    Checking the specific track avoids the false positive of a file with AC-3 on
    track 0 and AAC on track 1 passing as playable and playing silent."""
    out = await _run_capture(_bin("ffprobe", ffprobe), "-v", "error", "-select_streams", stream,
                             "-show_entries", "stream=codec_name",
                             "-of", "default=noprint_wrappers=1:nokey=1", str(path))
    lines = out.strip().splitlines()
    return lines[0].strip().lower() if lines else ""


async def probe_duration(path: Path, *, ffprobe: Optional[str] = None) -> float:
    out = await _run_capture(_bin("ffprobe", ffprobe), "-v", "error", "-show_entries",
                             "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
                             str(path))
    try:
        return float(out.strip() or 0)
    except ValueError:
        return 0.0


async def needs_audio_repair(path: Path, *, ffprobe: Optional[str] = None) -> bool:
    """True when the first audio track exists but is not browser-playable."""
    codec = await probe_codec(path, "a:0", ffprobe=ffprobe)
    return bool(codec) and codec not in PLAYABLE_AUDIO


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def _drain_progress(proc, total_s: float, cb: Optional[ProgressCb]) -> None:
    """Read ffmpeg's `-progress pipe:1`. It MUST always be drained, even without a
    callback: if nobody reads stdout the pipe fills, ffmpeg blocks on write and the
    caller waits forever."""
    start = time.monotonic()
    while True:
        line = await proc.stdout.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="ignore").strip()
        if cb is None or total_s <= 0 or not text.startswith("out_time_ms="):
            continue
        try:
            done_s = int(text.split("=", 1)[1]) / 1_000_000
        except ValueError:
            continue
        pct = max(0, min(99, int(done_s * 100 / total_s)))
        elapsed = time.monotonic() - start
        left = int((total_s - done_s) * (elapsed / done_s)) if done_s > 1 else 0
        try:
            cb(pct, left)
        except Exception as exc:  # noqa: BLE001 - a broken callback must not kill the encode
            log.debug("progress callback failed: %s", exc)


async def repair_audio(src: Path, dst: Optional[Path] = None, *,
                       progress_cb: Optional[ProgressCb] = None,
                       ffmpeg: Optional[str] = None, ffprobe: Optional[str] = None,
                       bitrate: str = "128k", low_priority: bool = True) -> Path:
    """Write `dst` (default: `<name>.fixed.mp4` beside `src`) with the video copied and
    the first audio track as AAC. Returns the path; raises `AudioRepairError`.

    Cancelling the awaiting task kills ffmpeg and removes the temp file."""
    src = Path(src)
    if not src.is_file():
        raise AudioRepairError(f"source missing: {src}")
    ffmpeg_bin, ffprobe_bin = _bin("ffmpeg", ffmpeg), _bin("ffprobe", ffprobe)
    dst = Path(dst) if dst else src.with_name(f"{src.stem}.fixed.mp4")
    if dst.resolve() == src.resolve():
        raise AudioRepairError("dst must differ from src")
    tmp = dst.with_name(dst.name + ".tmp_enc")
    tmp.unlink(missing_ok=True)

    cmd = [ffmpeg_bin, "-i", str(src), "-map", "0:v:0", "-map", "0:a:0",
           "-c:v", "copy", "-c:a", "aac", "-b:a", bitrate, "-movflags", "+faststart",
           "-progress", "pipe:1", "-nostats", "-f", "mp4", "-y", str(tmp)]
    total_s = await probe_duration(src, ffprobe=ffprobe_bin)
    flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) if low_priority else 0
    proc = None
    tasks: list = []
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=flags)
        drain = asyncio.create_task(_drain_progress(proc, total_s, progress_cb))
        tasks.append(drain)
        stderr = await proc.stderr.read()
        await proc.wait()
        await drain
        if proc.returncode != 0:
            tail = stderr.decode("utf-8", errors="ignore")[-800:]
            raise AudioRepairError(f"ffmpeg failed ({proc.returncode}): {tail}")
        if not tmp.is_file() or tmp.stat().st_size <= 0:
            raise AudioRepairError("ffmpeg produced an empty file")
        v = await probe_codec(tmp, "v:0", ffprobe=ffprobe_bin)
        a = await probe_codec(tmp, "a:0", ffprobe=ffprobe_bin)
        if not v or a != "aac":
            raise AudioRepairError(f"invalid output (video={v or '?'} audio={a or '?'})")
        os.replace(tmp, dst)
        return dst
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            _kill(proc)
            # Wait for the child to exit: on Windows it still holds the temp file open.
            await asyncio.shield(proc.wait())
        raise
    finally:
        for t in tasks:
            t.cancel()
        # Promoted files were moved by os.replace; anything left is garbage.
        tmp.unlink(missing_ok=True)
