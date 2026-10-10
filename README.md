# audiorepair

Make a video play in browsers and Chromecast by re-encoding **only the audio**.

Dolby (AC-3 / E-AC-3) and DTS audio does not play in Chrome or Chromecast: the
picture runs and the sound is silent. `audiorepair` copies the video stream
untouched (no quality loss, minutes instead of hours) and turns the first audio
track into AAC 128 kbps. The output is validated with ffprobe before it replaces
anything, and the source is never modified.

Extracted from the audio-fix worker of a production Telegram media bot.

## Install

```bash
pip install git+https://github.com/Kaio-Comk/audiorepair.git
```

Needs `ffmpeg` and `ffprobe` on `PATH` (or pass their paths).

## Use

```python
import asyncio
from pathlib import Path
from audiorepair import needs_audio_repair, repair_audio

async def main():
    src = Path("movie.mkv")
    if await needs_audio_repair(src):
        out = await repair_audio(src, progress_cb=lambda pct, eta_s: print(pct, eta_s))
        print("ready:", out)          # movie.fixed.mp4

asyncio.run(main())
```

- `needs_audio_repair(path)`: true when the first audio track is not AAC/MP3/Opus/Vorbis.
- `repair_audio(src, dst=None, ...)`: returns the new path or raises `AudioRepairError`.
- Cancelling the awaiting task **kills ffmpeg and removes the temp file**.
- `progress_cb(percent, seconds_left)` is optional; ffmpeg's progress pipe is
  always drained, so it can never block on a full pipe.
- It checks the *specific* track (`a:0`), so a file with AC-3 on track 0 and AAC
  on track 1 is not mistaken for a playable one.

Pair it with [`pg-lease-queue`](https://github.com/Kaio-Comk/pg-lease-queue) to run
repairs as durable background jobs that survive restarts.

## Why async subprocess

A blocking `subprocess.run(ffmpeg)` inside a coroutine freezes the event loop for
the whole encode. For us it stopped a Telegram client for 10 minutes; on return
it discarded the server's messages as too old and dropped the connection in the
middle of a 700 MB upload.

## Tests

```bash
pip install -e ".[dev]" && pytest      # uses a real ffmpeg; those tests skip without it
```

## License

MIT
