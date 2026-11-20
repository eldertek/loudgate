"""DSP invariant and verification tests for loudgate.clean."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy import signal

from loudgate.audio_io import AudioBuffer, write_audio
from loudgate.clean import (
    CleanConfig,
    build_tight_stft,
    clean_audio,
    clean_file,
    declick_signal,
    spectral_denoise,
    verify_parseval_energy,
    zero_phase_highpass,
)
from loudgate.probe import estimate_snr_and_floors


def test_stft_parseval_energy_conservation_and_wola_reconstruction() -> None:
    rng = np.random.default_rng(7)
    fs = 48000
    t = np.arange(fs, dtype=np.float64) / fs
    chirp = signal.chirp(t, f0=80.0, f1=12000.0, t1=1.0, method="logarithmic")
    awgn = 0.05 * rng.standard_normal(fs)
    x = 0.4 * chirp + awgn

    stft_obj = build_tight_stft(fs=fs, n_fft=2048, hop_length=512, window="hann")
    e_time, e_tf, rel_err = verify_parseval_energy(x, stft_obj)
    assert e_time > 0.0
    assert rel_err < 1e-12

    # Bypassed spectral denoise (strength=0.0) must reconstruct to machine precision.
    x_rec, bypass_err = spectral_denoise(x, fs=fs, n_fft=2048, hop_length=512, strength=0.0)
    assert bypass_err < 1e-12
    assert np.max(np.abs(x - x_rec)) < 1e-12


def test_zero_phase_highpass_attenuates_rumble_without_group_delay() -> None:
    fs = 48000
    t = np.arange(fs * 2, dtype=np.float64) / fs
    rumble = 0.5 * np.sin(2.0 * np.pi * 25.0 * t)
    tone = 0.4 * np.sin(2.0 * np.pi * 1000.0 * t)
    x = (rumble + tone)[:, np.newaxis]

    y = zero_phase_highpass(x, fs=fs, cutoff_hz=80.0, order=4)[:, 0]
    # Steady-state interior check
    sl = slice(fs // 2, 3 * fs // 2)
    residual_err = np.sqrt(np.mean((y[sl] - tone[sl]) ** 2))
    assert residual_err < 0.01

    # Cross-correlation peak at lag 0 confirms zero phase shift.
    corr = signal.correlate(y[sl], tone[sl], mode="same")
    center = len(corr) // 2
    assert abs(int(np.argmax(corr)) - center) == 0


def test_declick_repairs_isolated_impulses() -> None:
    fs = 48000
    t = np.arange(fs, dtype=np.float64) / fs
    clean = 0.25 * np.sin(2.0 * np.pi * 1000.0 * t)
    corrupted = clean.copy()
    click_positions = [5000, 18000, 32000]
    for pos in click_positions:
        corrupted[pos] += 0.95

    repaired, count = declick_signal(corrupted, threshold_mad=8.0, max_width=8)
    assert count == len(click_positions)
    assert np.max(np.abs(repaired - clean)) < 0.02


def test_spectral_denoise_improves_snr_on_chirp_plus_awgn(tmp_path: Path) -> None:
    rng = np.random.default_rng(99)
    fs = 48000
    t = np.arange(fs * 2, dtype=np.float64) / fs
    sig = np.zeros_like(t)
    # Active chirp burst with silent intervals before/after
    burst_t = np.arange(fs, dtype=np.float64) / fs
    sig[fs // 2 : fs // 2 + fs] = 0.35 * signal.chirp(burst_t, f0=250.0, f1=4000.0, t1=1.0, method="logarithmic")
    noisy = sig + 0.025 * rng.standard_normal(len(t)) + 0.03

    in_path = tmp_path / "noisy_chirp.wav"
    out_path = tmp_path / "cleaned_chirp.wav"
    write_audio(in_path, AudioBuffer(samples=noisy, sample_rate=fs), subtype="FLOAT")

    cfg = CleanConfig(
        highpass_hz=80.0,
        declick=True,
        denoise_strength=1.2,
        deess_enabled=True,
        gate_enabled=True,
        gate_threshold_db=-32.0,
    )
    stats = clean_file(in_path, out_path, config=cfg)
    assert stats.parseval_relative_error < 1e-12
    assert stats.noise_reduction_db_est > 6.0

    _, _, snr_in = estimate_snr_and_floors(noisy, fs)
    cleaned_buf, _ = clean_audio(AudioBuffer(samples=noisy, sample_rate=fs), config=cfg)
    _, _, snr_out = estimate_snr_and_floors(cleaned_buf.to_mono(), fs)
    assert snr_out > snr_in + 5.0
