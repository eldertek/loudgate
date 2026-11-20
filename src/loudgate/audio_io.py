"""Audio I/O and FFmpeg/FFprobe subprocess wrappers for loudgate."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

DIRECT_SOUNDFILE_EXTENSIONS = {".wav", ".flac", ".ogg", ".aiff", ".aif"}


@dataclass
class AudioBuffer:
    """Canonical 2D float64 audio buffer with explicit sampling frequency."""

    samples: np.ndarray
    sample_rate: int
    source_path: str | None = None
    subtype: str | None = None

    def __post_init__(self) -> None:
        arr = np.asarray(self.samples, dtype=np.float64)
        if arr.ndim == 1:
            arr = arr[:, np.newaxis]
        elif arr.ndim != 2:
            raise ValueError(f"Expected 1D or 2D audio array, got shape {arr.shape}")
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive, got {self.sample_rate}")
        self.samples = arr

    @property
    def n_samples(self) -> int:
        return int(self.samples.shape[0])

    @property
    def channels(self) -> int:
        return int(self.samples.shape[1])

    @property
    def duration_s(self) -> float:
        return float(self.n_samples / self.sample_rate)

    def to_mono(self) -> np.ndarray:
        """Return energy-preserving mono downmix (1D float64)."""
        if self.channels == 1:
            return self.samples[:, 0].copy()
        return np.mean(self.samples, axis=1)


def require_binary(name: str) -> str:
    """Locate an external binary on PATH or raise a clear RuntimeError."""
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeError(
            f"Required binary '{name}' was not found on PATH. "
            "Install FFmpeg and ensure ffmpeg/ffprobe are available."
        )
    return resolved


def run_ffmpeg(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Execute ffmpeg with UTF-8 capture and clean error reporting."""
    ffmpeg_bin = require_binary("ffmpeg")
    cmd = [ffmpeg_bin, "-hide_banner", "-nostats", *args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}: {proc.stderr.strip()}")
    return proc


def run_ffprobe(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Execute ffprobe with UTF-8 capture."""
    ffprobe_bin = require_binary("ffprobe")
    cmd = [ffprobe_bin, *args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe exited with code {proc.returncode}: {proc.stderr.strip()}")
    return proc


def ffprobe_metadata(path: str | Path) -> dict[str, Any]:
    """Read stream and container metadata via ffprobe."""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"Audio file not found: {src}")
    proc = run_ffprobe(
        [
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(src),
        ]
    )
    payload = json.loads(proc.stdout or "{}")
    audio_streams = [
        s for s in payload.get("streams", []) if s.get("codec_type") == "audio"
    ]
    stream = audio_streams[0] if audio_streams else {}
    fmt = payload.get("format", {})
    return {
        "path": str(src),
        "format_name": fmt.get("format_name"),
        "duration_s": float(fmt["duration"]) if "duration" in fmt else None,
        "bit_rate": int(fmt["bit_rate"]) if "bit_rate" in fmt else None,
        "codec_name": stream.get("codec_name"),
        "sample_rate": int(stream["sample_rate"]) if "sample_rate" in stream else None,
        "channels": int(stream["channels"]) if "channels" in stream else None,
    }


def read_audio(path: str | Path) -> AudioBuffer:
    """Read WAV/FLAC/OGG directly via soundfile, falling back to FFmpeg decode."""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"Audio file not found: {src}")

    if src.suffix.lower() in DIRECT_SOUNDFILE_EXTENSIONS:
        try:
            info = sf.info(str(src))
            data, fs = sf.read(str(src), dtype="float64", always_2d=True)
            return AudioBuffer(
                samples=data,
                sample_rate=int(fs),
                source_path=str(src),
                subtype=info.subtype,
            )
        except RuntimeError:
            pass

    with tempfile.TemporaryDirectory(prefix="loudgate_dec_") as tmpdir:
        wav_tmp = Path(tmpdir) / "decoded.wav"
        run_ffmpeg(
            [
                "-y",
                "-i",
                str(src),
                "-vn",
                "-c:a",
                "pcm_f32le",
                str(wav_tmp),
            ]
        )
        data, fs = sf.read(str(wav_tmp), dtype="float64", always_2d=True)
        return AudioBuffer(
            samples=data,
            sample_rate=int(fs),
            source_path=str(src),
            subtype="FLOAT",
        )


def write_audio(
    path: str | Path,
    audio: AudioBuffer,
    subtype: str = "PCM_24",
    bitrate: str = "192k",
) -> Path:
    """Write AudioBuffer to WAV/FLAC/OGG directly or transcode to MP3/AAC/M4A/OPUS."""
    dst = Path(path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    ext = dst.suffix.lower()

    if ext in DIRECT_SOUNDFILE_EXTENSIONS or ext == "":
        target = dst if ext != "" else dst.with_suffix(".wav")
        sf.write(str(target), audio.samples, audio.sample_rate, subtype=subtype)
        return target

    codec_map = {
        ".mp3": ["-c:a", "libmp3lame", "-b:a", bitrate],
        ".aac": ["-c:a", "aac", "-b:a", bitrate],
        ".m4a": ["-c:a", "aac", "-b:a", bitrate, "-movflags", "+faststart"],
        ".opus": ["-c:a", "libopus", "-b:a", bitrate],
    }
    if ext not in codec_map:
        raise ValueError(f"Unsupported output audio extension: {ext}")

    with tempfile.TemporaryDirectory(prefix="loudgate_enc_") as tmpdir:
        wav_tmp = Path(tmpdir) / "source.wav"
        sf.write(str(wav_tmp), audio.samples, audio.sample_rate, subtype="FLOAT")
        run_ffmpeg(
            [
                "-y",
                "-i",
                str(wav_tmp),
                *codec_map[ext],
                str(dst),
            ]
        )
    return dst
