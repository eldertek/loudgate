"""Two-pass EBU R128 mastering tests using real FFmpeg loudnorm and ebur128."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from scipy import signal

from loudgate.audio_io import AudioBuffer, write_audio
from loudgate.master import MasterConfig, apply_true_peak_limiter, master_file


def _make_speech_like_signal(fs: int = 48000, duration_s: float = 4.0) -> np.ndarray:
    rng = np.random.default_rng(42)
    n = int(fs * duration_s)
    t = np.arange(n, dtype=np.float64) / fs
    envelope = 0.5 * (1.0 + np.sin(2.0 * np.pi * 1.5 * t))
    carrier = (
        0.5 * np.sin(2.0 * np.pi * 220.0 * t)
        + 0.3 * signal.chirp(t, f0=350.0, f1=2200.0, t1=duration_s, method="logarithmic")
        + 0.05 * rng.standard_normal(n)
    )
    # Scale to quiet raw level (~ -28 LUFS)
    return 0.08 * envelope * carrier


@pytest.mark.parametrize(
    ("preset", "expected_lufs"),
    [
        ("podcast", -14.0),
        ("mobile", -16.0),
        ("broadcast", -23.0),
    ],
)
def test_master_presets_reach_target_lufs_and_true_peak_ceiling(
    tmp_path: Path,
    preset: str,
    expected_lufs: float,
) -> None:
    fs = 48000
    raw = _make_speech_like_signal(fs=fs, duration_s=4.0)
    src = tmp_path / "raw_voice.wav"
    dst = tmp_path / f"master_{preset}.wav"
    write_audio(src, AudioBuffer(samples=raw, sample_rate=fs), subtype="PCM_24")

    res = master_file(src, dst, MasterConfig(preset=preset, max_true_peak_dbtp=-1.0))
    assert dst.exists()
    assert abs(res.output_probe.integrated_lufs - expected_lufs) <= 1.0
    assert res.output_probe.true_peak_dbtp <= -1.0
    assert res.output_probe.oversampled_true_peak_dbtp <= -0.9


def test_master_mp3_export(tmp_path: Path) -> None:
    fs = 48000
    raw = _make_speech_like_signal(fs=fs, duration_s=3.0)
    src = tmp_path / "raw_mp3_in.wav"
    dst = tmp_path / "master_podcast.mp3"
    write_audio(src, AudioBuffer(samples=raw, sample_rate=fs), subtype="PCM_24")

    res = master_file(src, dst, MasterConfig(preset="podcast", max_true_peak_dbtp=-1.0, bitrate="192k"))
    assert dst.exists()
    assert abs(res.output_probe.integrated_lufs - (-14.0)) <= 1.2
    assert res.output_probe.true_peak_dbtp <= -1.0


def test_true_peak_limiter_clamps_oversampled_peaks() -> None:
    fs = 48000
    n = np.arange(fs, dtype=np.float64)
    hot = 1.4 * np.sin(2.0 * np.pi * (fs / 4.0) * (n / fs) + np.pi / 4.0)
    limited = apply_true_peak_limiter(AudioBuffer(samples=hot, sample_rate=fs), ceiling_dbtp=-1.0)
    up = signal.resample_poly(limited.samples[:, 0], up=4, down=1)
    tp_db = 20.0 * np.log10(np.max(np.abs(up)))
    assert tp_db <= -0.95
