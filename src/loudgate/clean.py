"""STFT spectral denoising, zero-phase filtering, de-clicker, de-esser, and gate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import ndimage, signal

from .audio_io import AudioBuffer, read_audio, write_audio

EPS = 1e-12


@dataclass
class CleanConfig:
    """Deterministic DSP cleanup configuration."""

    highpass_hz: float = 80.0
    highpass_order: int = 4
    remove_dc: bool = True
    declick: bool = True
    declick_threshold_mad: float = 8.0
    declick_max_width_samples: int = 8
    denoise_strength: float = 1.0
    spectral_floor_db: float = -28.0
    noise_profile_range_s: tuple[float, float] | None = None
    n_fft: int = 2048
    hop_length: int = 512
    window: str = "hann"
    deess_enabled: bool = False
    deess_low_hz: float = 5000.0
    deess_high_hz: float = 10000.0
    deess_threshold_db: float = -24.0
    deess_ratio: float = 3.0
    gate_enabled: bool = False
    gate_threshold_db: float = -45.0
    gate_range_db: float = -24.0
    gate_attack_ms: float = 5.0
    gate_release_ms: float = 80.0


@dataclass
class CleanStats:
    """Empirical DSP verification stats returned after cleaning."""

    sample_rate: int
    n_fft: int
    hop_length: int
    window: str
    highpass_hz: float
    clicks_repaired: int
    parseval_relative_error: float
    input_rms_dbfs: float
    output_rms_dbfs: float
    noise_reduction_db_est: float


def build_tight_stft(
    fs: int,
    n_fft: int = 2048,
    hop_length: int = 512,
    window: str = "hann",
) -> signal.ShortTimeFFT:
    """Construct a Parseval tight-frame ShortTimeFFT using a periodic root-Hann window."""
    if window.lower() != "hann":
        raise ValueError(f"Only 'hann' window is supported in clean-room tight-frame STFT, got '{window}'")
    if n_fft < 16 or hop_length < 1:
        raise ValueError(f"Invalid STFT parameters: n_fft={n_fft}, hop_length={hop_length}")
    if n_fft % hop_length != 0 or (n_fft // hop_length) < 2:
        raise ValueError(
            f"n_fft ({n_fft}) must be an integer multiple >= 2 of hop_length ({hop_length}) for COLA"
        )

    w_hann = signal.windows.hann(n_fft, sym=False)
    overlap = n_fft / hop_length
    # Periodic Hann has mean 0.5, so sum of shifted squared root-Hann equals 0.5 * overlap.
    win = np.sqrt(w_hann / (0.5 * overlap))
    return signal.ShortTimeFFT(win=win, hop=hop_length, fs=fs, mfft=n_fft, scale_to=None)


def verify_parseval_energy(
    signal_1d: np.ndarray,
    stft_obj: signal.ShortTimeFFT,
) -> tuple[float, float, float]:
    """Verify Parseval energy conservation between time domain and STFT domain."""
    x = np.asarray(signal_1d, dtype=np.float64)
    energy_time = float(np.sum(x * x))
    S = stft_obj.stft(x)

    weights = np.full(S.shape[0], 2.0, dtype=np.float64)
    weights[0] = 1.0
    if stft_obj.mfft % 2 == 0:
        weights[-1] = 1.0

    energy_tf = float(np.sum(weights[:, None] * (np.abs(S) ** 2)) / stft_obj.mfft)
    rel_err = abs(energy_time - energy_tf) / max(energy_time, 1e-18)
    return energy_time, energy_tf, float(rel_err)


def zero_phase_highpass(
    samples: np.ndarray,
    fs: int,
    cutoff_hz: float = 80.0,
    order: int = 4,
) -> np.ndarray:
    """Apply zero-phase Butterworth high-pass filter using second-order sections."""
    arr = np.asarray(samples, dtype=np.float64)
    if cutoff_hz <= 0.0 or cutoff_hz >= 0.45 * fs:
        return arr.copy()
    if arr.shape[0] < 16:
        return arr.copy()

    sos = signal.butter(order, cutoff_hz, btype="highpass", fs=fs, output="sos")
    padlen = 3 * (2 * len(sos) + 1)
    if arr.shape[0] <= padlen:
        return signal.sosfiltfilt(sos, arr, axis=0, padlen=max(0, arr.shape[0] - 1))
    return signal.sosfiltfilt(sos, arr, axis=0)


def declick_signal(
    samples: np.ndarray,
    threshold_mad: float = 8.0,
    max_width: int = 8,
) -> tuple[np.ndarray, int]:
    """Detect and linearly interpolate short isolated digital/impulse clicks."""
    arr = np.asarray(samples, dtype=np.float64).copy()
    is_1d = arr.ndim == 1
    if is_1d:
        arr = arr[:, np.newaxis]

    n_samples, n_channels = arr.shape
    if n_samples < 16:
        return (arr[:, 0] if is_1d else arr), 0

    repaired_total = 0
    for ch_idx in range(n_channels):
        ch = arr[:, ch_idx]
        ch_rms = float(np.sqrt(np.mean(ch * ch)))
        # Second finite difference isolates sharp impulses while attenuating low-freq content.
        d2 = np.zeros_like(ch)
        d2[1:-1] = ch[2:] - 2.0 * ch[1:-1] + ch[:-2]
        med = float(np.median(d2))
        mad = float(np.median(np.abs(d2 - med)))
        robust_scale = max(1.4826 * mad, 0.005 * ch_rms, 1e-5)

        # Require both high second-difference z-score and noticeable local jump vs neighbors.
        local_med = ndimage.median_filter(ch, size=max(7, 2 * max_width + 3), mode="nearest")
        local_diff = np.abs(ch - local_med)
        local_mad = max(1.4826 * float(np.median(local_diff)), 0.02 * ch_rms, 1e-5)

        spike_mask = (np.abs(d2 - med) > threshold_mad * robust_scale) & (
            local_diff > threshold_mad * local_mad
        )
        # Never interpolate endpoints where neighbor anchors do not exist.
        spike_mask[0] = False
        spike_mask[-1] = False

        if not np.any(spike_mask):
            continue

        labeled, n_clusters = ndimage.label(spike_mask)
        for cluster_id in range(1, n_clusters + 1):
            idx = np.flatnonzero(labeled == cluster_id)
            peak_dev = float(np.max(local_diff[idx]))
            core_idx = idx[local_diff[idx] >= 0.25 * peak_dev]
            if len(core_idx) == 0:
                continue
            # Keep contiguous core span if trimming edges split nothing.
            width = int(core_idx[-1] - core_idx[0] + 1)
            if 1 <= width <= max_width:
                left = int(core_idx[0]) - 1
                right = int(core_idx[-1]) + 1
                if left >= 0 and right < n_samples:
                    span = np.arange(core_idx[0], core_idx[-1] + 1)
                    ch[span] = np.interp(span, [left, right], [ch[left], ch[right]])
                    repaired_total += width

    return (arr[:, 0] if is_1d else arr), repaired_total


def _estimate_noise_psd(
    power_spec: np.ndarray,
    fs: int,
    hop_length: int,
    noise_profile_range_s: tuple[float, float] | None,
) -> np.ndarray:
    """Estimate per-bin noise power spectrum (shape: n_freqs x 1)."""
    n_frames = power_spec.shape[1]
    if n_frames == 0:
        return np.zeros((power_spec.shape[0], 1), dtype=np.float64)

    if noise_profile_range_s is not None:
        t0, t1 = noise_profile_range_s
        f0 = max(0, int(np.floor(t0 * fs / hop_length)))
        f1 = min(n_frames, int(np.ceil(t1 * fs / hop_length)))
        if f1 > f0:
            return np.mean(power_spec[:, f0:f1], axis=1, keepdims=True)

    frame_energies = np.sum(power_spec, axis=0)
    k = max(1, min(n_frames, int(np.ceil(0.15 * n_frames))))
    quiet_indices = np.argsort(frame_energies)[:k]
    return np.mean(power_spec[:, quiet_indices], axis=1, keepdims=True)


def spectral_denoise(
    samples: np.ndarray,
    fs: int,
    n_fft: int = 2048,
    hop_length: int = 512,
    strength: float = 1.0,
    floor_db: float = -28.0,
    noise_profile_range_s: tuple[float, float] | None = None,
) -> tuple[np.ndarray, float]:
    """STFT WOLA spectral Wiener denoising with time-frequency smoothing."""
    arr = np.asarray(samples, dtype=np.float64)
    is_1d = arr.ndim == 1
    if is_1d:
        arr = arr[:, np.newaxis]

    stft_obj = build_tight_stft(fs=fs, n_fft=n_fft, hop_length=hop_length, window="hann")
    _, _, parseval_err = verify_parseval_energy(arr[:, 0], stft_obj)

    if strength <= 0.0 or arr.shape[0] < 16:
        out = arr.copy()
        return (out[:, 0] if is_1d else out), parseval_err

    floor_gain = 10.0 ** (floor_db / 20.0)
    out = np.zeros_like(arr)

    for ch_idx in range(arr.shape[1]):
        ch = arr[:, ch_idx]
        S = stft_obj.stft(ch)
        P = np.abs(S) ** 2
        P_noise = _estimate_noise_psd(P, fs=fs, hop_length=hop_length, noise_profile_range_s=noise_profile_range_s)

        # Light 2D smoothing reduces musical noise artifacts while preserving transients.
        P_smooth = ndimage.uniform_filter(P, size=(3, 3), mode="nearest")
        alpha = float(strength)
        snr_post = P_smooth / (alpha * P_noise + EPS * EPS)
        wiener_gain = np.maximum(0.0, 1.0 - 1.0 / np.maximum(snr_post, 1.0))
        gain = np.maximum(floor_gain, wiener_gain)
        # Smooth gain over 3 frames to prevent abrupt spectral modulation.
        gain = ndimage.uniform_filter1d(gain, size=3, axis=1, mode="nearest")

        S_clean = S * gain
        rec = stft_obj.istft(S_clean, k1=len(ch))
        out[:, ch_idx] = rec[: len(ch)]

    return (out[:, 0] if is_1d else out), parseval_err


def _smooth_envelope(level: np.ndarray, fs: int, attack_ms: float, release_ms: float) -> np.ndarray:
    """Single-pole asymmetric attack/release envelope follower."""
    a_att = np.exp(-1.0 / max(1.0, (attack_ms * 1e-3) * fs))
    a_rel = np.exp(-1.0 / max(1.0, (release_ms * 1e-3) * fs))
    env = np.zeros_like(level)
    prev = 0.0
    for i, val in enumerate(level):
        coef = a_att if val > prev else a_rel
        prev = coef * prev + (1.0 - coef) * val
        env[i] = prev
    return env


def deess_signal(
    samples: np.ndarray,
    fs: int,
    low_hz: float = 5000.0,
    high_hz: float = 10000.0,
    threshold_db: float = -24.0,
    ratio: float = 3.0,
) -> np.ndarray:
    """Split-band zero-phase sibilance attenuator."""
    arr = np.asarray(samples, dtype=np.float64)
    nyq = 0.5 * fs
    hi = min(high_hz, 0.9 * nyq)
    lo = min(low_hz, hi - 200.0)
    if lo <= 50.0 or hi <= lo or arr.shape[0] < 32:
        return arr.copy()

    sos = signal.butter(4, [lo, hi], btype="bandpass", fs=fs, output="sos")
    sibilance = signal.sosfiltfilt(sos, arr, axis=0)
    thresh_lin = 10.0 ** (threshold_db / 20.0)
    out = arr.copy()

    for ch_idx in range(arr.shape[1]):
        sib_ch = sibilance[:, ch_idx]
        env = _smooth_envelope(np.abs(sib_ch), fs=fs, attack_ms=2.0, release_ms=40.0)
        env_db = 20.0 * np.log10(np.maximum(env, EPS))
        over_db = np.maximum(0.0, env_db - threshold_db)
        gain_db = -over_db * (1.0 - 1.0 / max(1.0, ratio))
        gain_lin = np.where(env > thresh_lin, 10.0 ** (gain_db / 20.0), 1.0)
        # Subtract only the attenuated excess of the isolated sibilant band.
        out[:, ch_idx] = arr[:, ch_idx] - sib_ch * (1.0 - gain_lin)
    return out


def noise_gate(
    samples: np.ndarray,
    fs: int,
    threshold_db: float = -45.0,
    range_db: float = -24.0,
    attack_ms: float = 5.0,
    release_ms: float = 80.0,
) -> np.ndarray:
    """Deterministic downward expander / noise gate with smooth envelope."""
    arr = np.asarray(samples, dtype=np.float64)
    if arr.shape[0] == 0:
        return arr.copy()

    floor_lin = 10.0 ** (range_db / 20.0)
    mono_abs = np.max(np.abs(arr), axis=1)
    env = _smooth_envelope(mono_abs, fs=fs, attack_ms=attack_ms, release_ms=release_ms)
    env_db = 20.0 * np.log10(np.maximum(env, EPS))

    # Smooth 6 dB knee below threshold down to range_db floor.
    knee = 6.0
    gain_db = np.zeros_like(env_db)
    below = env_db < threshold_db
    deficit = np.clip((threshold_db - env_db) / knee, 0.0, 1.0)
    gain_db[below] = range_db * deficit[below]
    gain = np.maximum(floor_lin, 10.0 ** (gain_db / 20.0))
    return arr * gain[:, np.newaxis]


def _rms_db(arr: np.ndarray) -> float:
    if arr.size == 0:
        return -120.0
    return float(max(-120.0, 20.0 * np.log10(max(float(np.sqrt(np.mean(arr * arr))), EPS))))


def clean_audio(
    audio: AudioBuffer,
    config: CleanConfig | None = None,
) -> tuple[AudioBuffer, CleanStats]:
    """Run the clean-room post-production cleanup chain on an AudioBuffer."""
    cfg = config or CleanConfig()
    x = audio.samples.copy()
    fs = audio.sample_rate
    input_rms_db = _rms_db(x)

    if cfg.remove_dc and x.shape[0] > 0:
        x = x - np.mean(x, axis=0, keepdims=True)

    if cfg.highpass_hz > 0.0:
        x = zero_phase_highpass(x, fs=fs, cutoff_hz=cfg.highpass_hz, order=cfg.highpass_order)

    clicks_repaired = 0
    if cfg.declick:
        x, clicks_repaired = declick_signal(
            x,
            threshold_mad=cfg.declick_threshold_mad,
            max_width=cfg.declick_max_width_samples,
        )

    x, parseval_err = spectral_denoise(
        x,
        fs=fs,
        n_fft=cfg.n_fft,
        hop_length=cfg.hop_length,
        strength=cfg.denoise_strength,
        floor_db=cfg.spectral_floor_db,
        noise_profile_range_s=cfg.noise_profile_range_s,
    )

    if cfg.deess_enabled:
        x = deess_signal(
            x,
            fs=fs,
            low_hz=cfg.deess_low_hz,
            high_hz=cfg.deess_high_hz,
            threshold_db=cfg.deess_threshold_db,
            ratio=cfg.deess_ratio,
        )

    if cfg.gate_enabled:
        x = noise_gate(
            x,
            fs=fs,
            threshold_db=cfg.gate_threshold_db,
            range_db=cfg.gate_range_db,
            attack_ms=cfg.gate_attack_ms,
            release_ms=cfg.gate_release_ms,
        )

    output_rms_db = _rms_db(x)
    # Estimate noise reduction on quietest 15% of 20 ms frames before/after.
    nr_est = 0.0
    frame_len = max(16, int(round(0.02 * fs)))
    n_frames = audio.n_samples // frame_len
    if n_frames >= 5:
        in_frames = np.mean(audio.samples[: n_frames * frame_len].reshape(n_frames, frame_len, -1) ** 2, axis=(1, 2))
        out_frames = np.mean(x[: n_frames * frame_len].reshape(n_frames, frame_len, -1) ** 2, axis=(1, 2))
        p_in = max(float(np.percentile(in_frames, 10)), EPS * EPS)
        p_out = max(float(np.percentile(out_frames, 10)), EPS * EPS)
        nr_est = float(10.0 * np.log10(p_in / p_out))

    cleaned = AudioBuffer(
        samples=x,
        sample_rate=fs,
        source_path=audio.source_path,
        subtype=audio.subtype,
    )
    stats = CleanStats(
        sample_rate=fs,
        n_fft=cfg.n_fft,
        hop_length=cfg.hop_length,
        window=cfg.window,
        highpass_hz=cfg.highpass_hz,
        clicks_repaired=clicks_repaired,
        parseval_relative_error=parseval_err,
        input_rms_dbfs=input_rms_db,
        output_rms_dbfs=output_rms_db,
        noise_reduction_db_est=nr_est,
    )
    return cleaned, stats


def clean_file(
    input_path: str | Path,
    output_path: str | Path,
    config: CleanConfig | None = None,
    subtype: str = "PCM_24",
) -> CleanStats:
    """Load an audio file, apply cleanup chain, and write output."""
    buf = read_audio(input_path)
    cleaned, stats = clean_audio(buf, config=config)
    write_audio(output_path, cleaned, subtype=subtype)
    return stats
