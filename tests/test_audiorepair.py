import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from audiorepair import (AudioRepairError, needs_audio_repair, probe_codec, probe_duration,
                         repair_audio)

FFMPEG = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed")


def _make_clip(path: Path, audio_codec: str) -> None:
    subprocess.run(
        [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", audio_codec, "-y", str(path)],
        check=True)


@needs_ffmpeg
def test_ac3_vira_aac_e_video_e_copiado(tmp_path):
    src = tmp_path / "in.mp4"
    _make_clip(src, "ac3")

    async def go():
        assert await needs_audio_repair(src, ffprobe=FFPROBE) is True
        seen = []
        out = await repair_audio(src, ffmpeg=FFMPEG, ffprobe=FFPROBE,
                                 progress_cb=lambda p, s: seen.append(p))
        return out

    out = asyncio.run(go())
    assert out.name == "in.fixed.mp4" and out.is_file()
    assert asyncio.run(probe_codec(out, "a:0", ffprobe=FFPROBE)) == "aac"
    assert asyncio.run(probe_codec(out, "v:0", ffprobe=FFPROBE)) == "h264"
    assert asyncio.run(probe_duration(out, ffprobe=FFPROBE)) > 1.5
    assert asyncio.run(probe_codec(src, "a:0", ffprobe=FFPROBE)) == "ac3"   # origem intacta
    assert not list(tmp_path.glob("*.tmp_enc"))


@needs_ffmpeg
def test_arquivo_ja_compativel_nao_precisa_de_reparo(tmp_path):
    src = tmp_path / "ok.mp4"
    _make_clip(src, "aac")
    assert asyncio.run(needs_audio_repair(src, ffprobe=FFPROBE)) is False


@needs_ffmpeg
def test_entrada_corrompida_levanta_erro_e_nao_deixa_lixo(tmp_path):
    src = tmp_path / "lixo.mp4"
    src.write_bytes(b"not a video")
    with pytest.raises(AudioRepairError):
        asyncio.run(repair_audio(src, ffmpeg=FFMPEG, ffprobe=FFPROBE))
    assert not list(tmp_path.glob("*.tmp_enc")) and not (tmp_path / "lixo.fixed.mp4").exists()


def test_origem_ausente_e_dst_igual_a_src(tmp_path):
    with pytest.raises(AudioRepairError, match="source missing"):
        asyncio.run(repair_audio(tmp_path / "nao-existe.mp4", ffmpeg="x", ffprobe="y"))
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    with pytest.raises(AudioRepairError, match="differ"):
        asyncio.run(repair_audio(f, f, ffmpeg="x", ffprobe="y"))


def test_binario_ausente_da_erro_claro(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    with pytest.raises(AudioRepairError, match="not found"):
        asyncio.run(repair_audio(f))


@needs_ffmpeg
def test_cancelar_mata_o_ffmpeg_e_limpa_o_temporario(tmp_path):
    src = tmp_path / "long.mp4"
    subprocess.run(
        [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=60:size=640x480:rate=30",
         "-f", "lavfi", "-i", "sine=duration=60", "-c:v", "libx264", "-preset", "veryslow",
         "-c:a", "ac3", "-y", str(src)], check=True)

    async def go():
        task = asyncio.create_task(repair_audio(src, ffmpeg=FFMPEG, ffprobe=FFPROBE))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert not list(tmp_path.glob("*.tmp_enc")) and not (tmp_path / "long.fixed.mp4").exists()
