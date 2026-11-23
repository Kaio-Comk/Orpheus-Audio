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


@needs_ffmpeg
def test_dois_reparos_simultaneos_do_mesmo_arquivo_nao_disputam_o_temporario(tmp_path):
    src = tmp_path / "in.mp4"
    _make_clip(src, "ac3")

    async def go():
        return await asyncio.gather(
            repair_audio(src, tmp_path / "a.mp4", ffmpeg=FFMPEG, ffprobe=FFPROBE),
            repair_audio(src, tmp_path / "a.mp4", ffmpeg=FFMPEG, ffprobe=FFPROBE),
            return_exceptions=True)

    results = asyncio.run(go())
    assert all(isinstance(r, Path) for r in results), results
    assert asyncio.run(probe_codec(tmp_path / "a.mp4", "a:0", ffprobe=FFPROBE)) == "aac"
    assert not list(tmp_path.glob("*.tmp_enc"))


@needs_ffmpeg
def test_caminho_relativo_comecando_com_hifen_nao_vira_opcao(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_clip(tmp_path / "-y.mp4", "ac3")
    src = Path("-y.mp4")
    assert asyncio.run(probe_codec(src, "a:0", ffprobe=FFPROBE)) == "ac3"
    out = asyncio.run(repair_audio(src, ffmpeg=FFMPEG, ffprobe=FFPROBE))
    assert asyncio.run(probe_codec(out, "a:0", ffprobe=FFPROBE)) == "aac"


@needs_ffmpeg
def test_temporario_esquecido_por_processo_morto_e_limpo_so_quando_velho(tmp_path):
    """A process killed hard leaves `<dst>.<id>.tmp_enc` behind. Leftovers older than a day are removed
    on the next repair of the same dst; a recent one (maybe a repair still running) is kept."""
    import time
    src = tmp_path / "in.mp4"
    _make_clip(src, "ac3")
    dst = tmp_path / "out.mp4"
    velho = tmp_path / "out.mp4.deadbeef.tmp_enc"
    legado = tmp_path / "out.mp4.tmp_enc"                 # name used before 04/11/2026
    recente = tmp_path / "out.mp4.cafecafe.tmp_enc"
    outro = tmp_path / "other.mp4.deadbeef.tmp_enc"       # belongs to another dst: not touched
    for f in (velho, legado, recente, outro):
        f.write_bytes(b"x")
    dois_dias = time.time() - 2 * 24 * 3600
    for f in (velho, legado, outro):
        os.utime(f, (dois_dias, dois_dias))
    asyncio.run(repair_audio(src, dst, ffmpeg=FFMPEG, ffprobe=FFPROBE))
    assert dst.is_file()
    assert not velho.exists() and not legado.exists()
    assert recente.exists() and outro.exists()
