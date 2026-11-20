"""Audio diagnostics and EBU R128 loudness audit for loudgate."""

from __future__ import annotations

import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy import signal

from .audio_io import AudioBuffer, read_audio, run_ffmpeg, write_audio

EPS = 1e-12
MIN_DB = -120.0


@dataclass
class ProbeReport:
    """Comprehensive loudness, peak, DC, clipping, and SNR diagnostics."""

    path: str
    sample_rate: int
    channels: int
    n_samples: int
    duration_s: float
    integrated_lufs: float
    threshold_lufs: float
    lra_lu: float
    lra_low_lufs: float
    lra_high_lufs: float
    ffmpeg_sample_peak_dbfs: float
    true_peak_dbtp: float
    sample_peak_dbfs: float
    oversampled_true_peak_dbtp: float
    inter_sample_peak_excess_db: float
    has_inter_sample_clipping: bool
    rms_dbfs: float
    crest_factor_db: float
    dc_offset_per_channel: list[float]
    max_abs_dc_offset: float
    clipped_samples: int
    clipped_ratio: float
    has_clipping: bool
    noise_floor_dbfs: float
    signal_level_dbfs: float
    estimated_snr_db: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _amp_to_db(amp: float) -> float:
    return float(max(MIN_DB, 20.0 * np.log10(max(amp, EPS))))


def _power_to_db(power: float) -> float:
    return float(max(MIN_DB, 10.0 * np.log10(max(power, EPS * EPS))))


def parse_ebur128_summary(stderr_text: str) -> dict[str, float]:
    """Parse the Summary block emitted by FFmpeg's ebur128=peak=sample+true filter."""
    summary_idx = stderr_text.rfind("Summary:")
    block = stderr_text[summary_idx:] if summary_idx != -1 else stderr_text

    def _extract(pattern: str, default: float = -70.0) -> float:
        match = re.search(pattern, block)
        if not match:
            return default
        raw = match.group(1).strip()
        if raw.lower() in {"-inf", "-inf.", "nan"}:
            return default
        return float(raw)

    return {
        "integrated_lufs": _extract(r"Integrated loudness:\s*\r?\n\s*I:\s*([+-]?\d+(?:\.\d+)?|-inf)\s*LUFS"),
        "threshold_lufs": _extract(r"Integrated loudness:[\s\S]*?Threshold:\s*([+-]?\d+(?:\.\d+)?|-inf)\s*LUFS"),
        "lra_lu": _extract(r"Loudness range:\s*\r?\n\s*LRA:\s*([+-]?\d+(?:\.\d+)?|-inf)\s*LU", 0.0),
        "lra_low_lufs": _extract(r"LRA low:\s*([+-]?\d+(?:\.\d+)?|-inf)\s*LUFS"),
        "lra_high_lufs": _extract(r"LRA high:\s*([+-]?\d+(?:\.\d+)?|-inf)\s*LUFS"),
        "sample_peak_dbfs": _extract(r"Sample peak:\s*\r?\n\s*Peak:\s*([+-]?\d+(?:\.\d+)?|-inf)\s*dBFS", MIN_DB),
        "true_peak_dbtp": _extract(r"True peak:\s*\r?\n\s*Peak:\s*([+-]?\d+(?:\.\d+)?|-inf)\s*dBFS", MIN_DB),
    }


def run_ebur128_ffmpeg(path: str | Path) -> dict[str, float]:
    """Run FFmpeg EBU R128 loudness and true-peak scan on an audio file."""
    proc = run_ffmpeg(
        [
            "-i",
            str(path),
            "-vn",
            "-af",
            "ebur128=peak=sample+true",
            "-f",
            "null",
            "-",
        ]
    )
    return parse_ebur128_summary(proc.stderr)


def estimate_snr_and_floors(mono: np.ndarray, fs: int) -> tuple[float, float, float]:
    """Estimate noise floor (dBFS), signal level (dBFS), and SNR (dB).

    Uses 20 ms frame energy quantiles for speech/intermittent signals and falls back
    to Welch PSD noise-floor estimation when the signal envelope is stationary.
    """
    if len(mono) == 0:
        return MIN_DB, MIN_DB, 0.0

    # Remove DC before energy/PSD estimation so DC offset does not bias noise floor.
    x = mono - np.mean(mono)
    total_power = float(np.mean(x * x))
    if total_power <= EPS * EPS:
        return MIN_DB, MIN_DB, 0.0

    frame_len = max(16, int(round(0.02 * fs)))
    n_frames = len(x) // frame_len
    if n_frames >= 5:
        frames = x[: n_frames * frame_len].reshape(n_frames, frame_len)
        frame_power = np.mean(frames * frames, axis=1)
        p10 = float(np.percentile(frame_power, 10))
        p90 = float(np.percentile(frame_power, 90))

        # Intermittent / speech-like signal with quiet intervals (dynamic range > 6 dB).
        if p90 > 4.0 * max(p10, EPS * EPS):
            active_mask = frame_power > max(p10 * 2.0, EPS * EPS)
            signal_power = float(np.mean(frame_power[active_mask])) if np.any(active_mask) else p90
            noise_power = max(p10, EPS * EPS)
            pure_signal_power = max(signal_power - noise_power, EPS * EPS)
            snr_db = 10.0 * np.log10(pure_signal_power / noise_power)
            return _power_to_db(noise_power), _power_to_db(signal_power), float(snr_db)

    # Stationary signal (e.g., continuous sine or steady noise): Welch PSD median estimator.
    nperseg = min(len(x), 2048)
    freqs, psd = signal.welch(x, fs=fs, window="hann", nperseg=nperseg, scaling="density")
    if len(psd) > 2:
        # Median PSD scaled by Nyquist bandwidth estimates broadband noise power.
        noise_psd_median = float(np.median(psd[1:-1]))
        noise_power = max(noise_psd_median * (fs / 2.0) / np.log(2.0), EPS * EPS)
        # Note: for an exponential/chi2(2) periodogram, median = mean * ln(2);
        # Welch averaging reduces variance so median approaches mean as n_avg grows.
        # Clamp noise_power to not exceed total_power for pure broadband noise.
        noise_power = min(noise_power, total_power)
        if total_power > noise_power * 1.05:
            pure_signal_power = max(total_power - noise_power, EPS * EPS)
            snr_db = 10.0 * np.log10(pure_signal_power / noise_power)
            return _power_to_db(noise_power), _power_to_db(total_power), float(snr_db)

    return _power_to_db(total_power), _power_to_db(total_power), 0.0


def probe_buffer(
    audio: AudioBuffer,
    ebur128_stats: dict[str, float] | None = None,
    clip_threshold: float = 0.999,
) -> ProbeReport:
    """Compute DSP diagnostics and EBU R128 loudness metrics on an AudioBuffer."""
    x = audio.samples
    fs = audio.sample_rate

    if ebur128_stats is None:
        with tempfile.TemporaryDirectory(prefix="loudgate_probe_") as tmpdir:
            tmp_wav = Path(tmpdir) / "probe.wav"
            write_audio(tmp_wav, audio, subtype="FLOAT")
            ebur128_stats = run_ebur128_ffmpeg(tmp_wav)

    if audio.n_samples == 0:
        sample_peak = 0.0
        oversampled_peak = 0.0
        rms = 0.0
        dc_offsets = [0.0] * audio.channels
        clipped_count = 0
        clipped_ratio = 0.0
    else:
        abs_x = np.abs(x)
        sample_peak = float(np.max(abs_x))
        # 4x polyphase oversampling per channel for true-peak / inter-sample peak check.
        os_peaks = []
        for ch in range(audio.channels):
            if audio.n_samples >= 8:
                up = signal.resample_poly(x[:, ch], up=4, down=1)
                os_peaks.append(float(np.max(np.abs(up))))
            else:
                os_peaks.append(float(np.max(abs_x[:, ch])))
        oversampled_peak = max(os_peaks) if os_peaks else 0.0
        rms = float(np.sqrt(np.mean(x * x)))
        dc_offsets = [float(np.mean(x[:, ch])) for ch in range(audio.channels)]
        clipped_mask = np.any(abs_x >= clip_threshold, axis=1)
        clipped_count = int(np.sum(clipped_mask))
        clipped_ratio = float(clipped_count / audio.n_samples)

    sample_peak_db = _amp_to_db(sample_peak)
    oversampled_peak_db = _amp_to_db(oversampled_peak)
    rms_db = _amp_to_db(rms)
    inter_sample_excess = max(0.0, oversampled_peak_db - sample_peak_db)
    noise_floor_db, signal_level_db, snr_db = estimate_snr_and_floors(audio.to_mono(), fs)

    return ProbeReport(
        path=audio.source_path or "<memory>",
        sample_rate=fs,
        channels=audio.channels,
        n_samples=audio.n_samples,
        duration_s=audio.duration_s,
        integrated_lufs=ebur128_stats["integrated_lufs"],
        threshold_lufs=ebur128_stats["threshold_lufs"],
        lra_lu=ebur128_stats["lra_lu"],
        lra_low_lufs=ebur128_stats["lra_low_lufs"],
        lra_high_lufs=ebur128_stats["lra_high_lufs"],
        ffmpeg_sample_peak_dbfs=ebur128_stats["sample_peak_dbfs"],
        true_peak_dbtp=ebur128_stats["true_peak_dbtp"],
        sample_peak_dbfs=sample_peak_db,
        oversampled_true_peak_dbtp=oversampled_peak_db,
        inter_sample_peak_excess_db=float(inter_sample_excess),
        has_inter_sample_clipping=bool(oversampled_peak_db > 0.0),
        rms_dbfs=rms_db,
        crest_factor_db=float(sample_peak_db - rms_db),
        dc_offset_per_channel=dc_offsets,
        max_abs_dc_offset=float(max((abs(v) for v in dc_offsets), default=0.0)),
        clipped_samples=clipped_count,
        clipped_ratio=clipped_ratio,
        has_clipping=bool(clipped_count > 0),
        noise_floor_dbfs=noise_floor_db,
        signal_level_dbfs=signal_level_db,
        estimated_snr_db=snr_db,
    )


def probe_file(path: str | Path, clip_threshold: float = 0.999) -> ProbeReport:
    """Audit an audio file with FFmpeg EBU R128 and sample-level DSP diagnostics."""
    src = Path(path)
    ebur128_stats = run_ebur128_ffmpeg(src)
    audio = read_audio(src)
    return probe_buffer(audio, ebur128_stats=ebur128_stats, clip_threshold=clip_threshold)
