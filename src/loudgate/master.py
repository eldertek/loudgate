"""Two-pass EBU R128 loudness normalization and true-peak limiting."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage, signal

from .audio_io import AudioBuffer, ffprobe_metadata, read_audio, run_ffmpeg, write_audio
from .probe import ProbeReport, probe_file

MASTER_PRESETS: dict[str, dict[str, float]] = {
    "web": {"target_lufs": -14.0, "max_true_peak_dbtp": -1.0, "target_lra": 11.0},
    "podcast": {"target_lufs": -14.0, "max_true_peak_dbtp": -1.0, "target_lra": 11.0},
    "mobile": {"target_lufs": -16.0, "max_true_peak_dbtp": -1.0, "target_lra": 9.0},
    "stereo": {"target_lufs": -16.0, "max_true_peak_dbtp": -1.0, "target_lra": 9.0},
    "broadcast": {"target_lufs": -23.0, "max_true_peak_dbtp": -1.0, "target_lra": 7.0},
    "ebu_r128": {"target_lufs": -23.0, "max_true_peak_dbtp": -1.0, "target_lra": 7.0},
}


@dataclass
class MasterConfig:
    """Broadcast loudness normalization and export settings."""

    preset: str = "podcast"
    target_lufs: float | None = None
    max_true_peak_dbtp: float = -1.0
    target_lra: float | None = None
    bitrate: str = "192k"
    sample_rate: int | None = None
    pcm_subtype: str = "PCM_24"
    dual_mono: bool = False


@dataclass
class MasterResult:
    """Two-pass mastering report with verified post-render probe metrics."""

    input_path: str
    output_path: str
    preset: str
    target_lufs: float
    max_true_peak_dbtp: float
    target_lra: float
    loudnorm_mode: str
    pass1_stats: dict[str, Any]
    pass2_stats: dict[str, Any]
    output_probe: ProbeReport

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


def _parse_loudnorm_json(stderr_text: str) -> dict[str, Any]:
    """Extract the JSON block emitted by FFmpeg loudnorm=print_format=json."""
    matches = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr_text, flags=re.DOTALL)
    if not matches:
        raise RuntimeError(f"Could not locate loudnorm JSON in FFmpeg output:\n{stderr_text[-800:]}")
    return json.loads(matches[-1])


def _is_finite_lufs(raw_val: Any) -> bool:
    try:
        val = float(raw_val)
    except (TypeError, ValueError):
        return False
    return np.isfinite(val) and val > -70.0


def apply_true_peak_limiter(
    audio: AudioBuffer,
    ceiling_dbtp: float = -1.0,
    lookahead_ms: float = 2.0,
    release_ms: float = 50.0,
) -> AudioBuffer:
    """4x-oversampled lookahead true-peak limiter for PCM safety ceiling."""
    x = audio.samples.copy()
    fs = audio.sample_rate
    if x.shape[0] < 8:
        return audio

    ceiling_lin = 10.0 ** (ceiling_dbtp / 20.0)
    # Detect inter-sample envelope using 4x polyphase oversampling.
    os_peak = np.zeros(x.shape[0], dtype=np.float64)
    for ch_idx in range(x.shape[1]):
        up = np.abs(signal.resample_poly(x[:, ch_idx], up=4, down=1))
        ch_peak = up[: x.shape[0] * 4].reshape(x.shape[0], 4).max(axis=1)
        os_peak = np.maximum(os_peak, ch_peak)

    if float(np.max(os_peak)) <= ceiling_lin:
        return audio

    # Smooth peak envelope with lookahead dilation and exponential release.
    look_samples = max(1, int(round(lookahead_ms * 1e-3 * fs)))
    dilated = ndimage.maximum_filter1d(os_peak, size=2 * look_samples + 1, mode="nearest")
    desired_gain = np.minimum(1.0, ceiling_lin / np.maximum(dilated, 1e-12))

    # Smooth gain recovery with release time constant.
    a_rel = np.exp(-1.0 / max(1.0, release_ms * 1e-3 * fs))
    smoothed_gain = np.empty_like(desired_gain)
    g = 1.0
    for i, target_g in enumerate(desired_gain):
        if target_g < g:
            g = target_g
        else:
            g = a_rel * g + (1.0 - a_rel) * target_g
        smoothed_gain[i] = min(g, target_g)

    limited = x * smoothed_gain[:, np.newaxis]
    return AudioBuffer(
        samples=limited,
        sample_rate=fs,
        source_path=audio.source_path,
        subtype=audio.subtype,
    )


def _codec_args_for_output(dst: Path, pcm_subtype: str, bitrate: str) -> list[str]:
    ext = dst.suffix.lower()
    if ext in {".wav", ""}:
        codec = {"PCM_16": "pcm_s16le", "PCM_24": "pcm_s24le", "FLOAT": "pcm_f32le"}.get(
            pcm_subtype.upper(), "pcm_s24le"
        )
        return ["-c:a", codec]
    if ext == ".flac":
        return ["-c:a", "flac"]
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-b:a", bitrate]
    if ext == ".aac":
        return ["-c:a", "aac", "-b:a", bitrate]
    if ext == ".m4a":
        return ["-c:a", "aac", "-b:a", bitrate, "-movflags", "+faststart"]
    if ext == ".opus":
        return ["-c:a", "libopus", "-b:a", bitrate]
    raise ValueError(f"Unsupported mastering output format: {ext}")


def master_file(
    input_path: str | Path,
    output_path: str | Path,
    config: MasterConfig | None = None,
) -> MasterResult:
    """Run two-pass EBU R128 mastering with true-peak ceiling verification."""
    cfg = config or MasterConfig()
    preset_key = cfg.preset.lower()
    if preset_key not in MASTER_PRESETS:
        valid = ", ".join(sorted(MASTER_PRESETS))
        raise ValueError(f"Unknown mastering preset '{cfg.preset}'. Valid presets: {valid}")

    preset_vals = MASTER_PRESETS[preset_key]
    target_lufs = float(cfg.target_lufs if cfg.target_lufs is not None else preset_vals["target_lufs"])
    max_tp = float(cfg.max_true_peak_dbtp)
    target_lra = float(cfg.target_lra if cfg.target_lra is not None else preset_vals["target_lra"])

    src = Path(input_path)
    dst = Path(output_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        raise FileNotFoundError(f"Input audio file not found: {src}")

    meta = ffprobe_metadata(src)
    out_sr = int(cfg.sample_rate or meta.get("sample_rate") or 48000)

    # Slightly tighten filter TP ceiling for lossy encoders to prevent codec overshoot.
    is_lossy = dst.suffix.lower() in {".mp3", ".aac", ".m4a", ".opus"}
    filter_tp = max(-9.0, max_tp - (0.3 if is_lossy else 0.05))
    dual_mono_opt = ":dual_mono=true" if cfg.dual_mono else ""

    pass1_filter = (
        f"loudnorm=I={target_lufs:.2f}:TP={filter_tp:.2f}:LRA={target_lra:.2f}"
        f"{dual_mono_opt}:print_format=json"
    )
    p1 = run_ffmpeg(["-i", str(src), "-vn", "-af", pass1_filter, "-f", "null", "-"])
    pass1_stats = _parse_loudnorm_json(p1.stderr)

    use_two_pass = all(
        _is_finite_lufs(pass1_stats.get(k))
        for k in ("input_i", "input_tp", "input_lra", "input_thresh")
    )

    if use_two_pass:
        pass2_filter = (
            f"loudnorm=I={target_lufs:.2f}:TP={filter_tp:.2f}:LRA={target_lra:.2f}"
            f":measured_I={pass1_stats['input_i']}"
            f":measured_TP={pass1_stats['input_tp']}"
            f":measured_LRA={pass1_stats['input_lra']}"
            f":measured_thresh={pass1_stats['input_thresh']}"
            f":offset={pass1_stats.get('target_offset', '0.0')}"
            f":linear=true{dual_mono_opt}:print_format=json"
        )
        mode = "two_pass_linear"
    else:
        pass2_filter = pass1_filter
        mode = "single_pass_fallback"

    codec_args = _codec_args_for_output(dst, pcm_subtype=cfg.pcm_subtype, bitrate=cfg.bitrate)
    p2 = run_ffmpeg(
        [
            "-y",
            "-i",
            str(src),
            "-vn",
            "-af",
            pass2_filter,
            "-ar",
            str(out_sr),
            *codec_args,
            str(dst),
        ]
    )
    pass2_stats = _parse_loudnorm_json(p2.stderr)
    out_probe = probe_file(dst)

    # Deterministic PCM safety limiter if true peak exceeds target ceiling.
    if out_probe.true_peak_dbtp > max_tp or out_probe.oversampled_true_peak_dbtp > max_tp:
        if not is_lossy:
            buf = read_audio(dst)
            limited = apply_true_peak_limiter(buf, ceiling_dbtp=max_tp - 0.05)
            write_audio(dst, limited, subtype=cfg.pcm_subtype, bitrate=cfg.bitrate)
            out_probe = probe_file(dst)
            mode = f"{mode}+pcm_true_peak_limiter"

    return MasterResult(
        input_path=str(src),
        output_path=str(dst),
        preset=preset_key,
        target_lufs=target_lufs,
        max_true_peak_dbtp=max_tp,
        target_lra=target_lra,
        loudnorm_mode=mode,
        pass1_stats=pass1_stats,
        pass2_stats=pass2_stats,
        output_probe=out_probe,
    )
