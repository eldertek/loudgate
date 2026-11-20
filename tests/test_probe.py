"""Synthetic DSP vector tests for loudgate.probe."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from loudgate.audio_io import AudioBuffer, write_audio
from loudgate.probe import probe_buffer, probe_file


def test_probe_pure_sine_ebur128_and_metrics(tmp_path: Path) -> None:
    fs = 48000
    t = np.arange(fs * 2, dtype=np.float64) / fs
    amp = 10.0 ** (-18.0 / 20.0)
    sine = amp * np.sin(2.0 * np.pi * 1000.0 * t)
    wav_path = tmp_path / "tone_1k.wav"
    write_audio(wav_path, AudioBuffer(samples=sine, sample_rate=fs), subtype="FLOAT")

    report = probe_file(wav_path)
    assert report.sample_rate == fs
    assert report.channels == 1
    assert abs(report.sample_peak_dbfs - (-18.0)) < 0.05
    assert abs(report.rms_dbfs - (-21.01)) < 0.05
    assert abs(report.crest_factor_db - 3.01) < 0.05
    assert report.max_abs_dc_offset < 1e-6
    assert report.clipped_samples == 0
    assert not report.has_clipping
    # Real FFmpeg ebur128 integrated loudness for 1 kHz sine at -18 dBFS peak (~ -21.0 LUFS)
    assert -22.5 <= report.integrated_lufs <= -19.5
    assert report.estimated_snr_db > 40.0


def test_probe_detects_dc_offset_and_clipping(tmp_path: Path) -> None:
    fs = 48000
    t = np.arange(fs, dtype=np.float64) / fs
    ch0 = np.clip(1.4 * np.sin(2.0 * np.pi * 440.0 * t), -1.0, 1.0) + 0.12
    ch1 = 0.2 * np.sin(2.0 * np.pi * 440.0 * t) - 0.05
    stereo = np.column_stack([np.clip(ch0, -1.0, 1.0), ch1])
    wav_path = tmp_path / "clipped_dc.wav"
    write_audio(wav_path, AudioBuffer(samples=stereo, sample_rate=fs), subtype="FLOAT")

    report = probe_file(wav_path)
    assert report.has_clipping
    assert report.clipped_samples > 100
    assert report.max_abs_dc_offset > 0.04
    assert report.dc_offset_per_channel[1] < -0.04


def test_probe_detects_inter_sample_peak() -> None:
    fs = 48000
    n = np.arange(fs, dtype=np.float64)
    # Fs/4 tone sampled at 45-degree phase: samples are +/- 0.99 / sqrt(2) * sqrt(2)?
    # Specifically, peak-aligned Fs/4 sine with amplitude 1.25 at phase pi/4 gives
    # discrete samples +/- 1.25 * sin(pi/4) = +/- 0.8839 (< 0 dBFS), while continuous peak is +1.94 dBTP!
    x = 1.25 * np.sin(2.0 * np.pi * (fs / 4.0) * (n / fs) + np.pi / 4.0)
    buf = AudioBuffer(samples=x, sample_rate=fs)
    report = probe_buffer(buf)
    assert report.sample_peak_dbfs < -0.5
    assert report.oversampled_true_peak_dbtp > 0.5
    assert report.inter_sample_peak_excess_db > 1.5
    assert report.has_inter_sample_clipping


def test_probe_snr_estimation_on_pulsed_signal_with_awgn() -> None:
    rng = np.random.default_rng(1234)
    fs = 48000
    t = np.arange(fs * 2, dtype=np.float64) / fs
    signal_clean = np.zeros_like(t)
    # Active bursts over 50% of duration
    burst = 0.2 * np.sin(2.0 * np.pi * 600.0 * t[: fs // 2])
    signal_clean[fs // 4 : 3 * fs // 4] = burst
    signal_clean[fs + fs // 4 : fs + 3 * fs // 4] = burst

    # Set active burst power vs AWGN to ~ 20 dB SNR
    burst_power = float(np.mean(burst * burst))
    noise_std = np.sqrt(burst_power / (10.0 ** (20.0 / 10.0)))
    noisy = signal_clean + rng.normal(0.0, noise_std, size=len(t))

    report = probe_buffer(AudioBuffer(samples=noisy, sample_rate=fs))
    assert 17.0 <= report.estimated_snr_db <= 23.0
