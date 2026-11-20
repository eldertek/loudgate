"""Energy/ZCR VAD silence detector, FFmpeg trim, OTIO/EDL, and SRT/VTT export."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .audio_io import AudioBuffer, read_audio, run_ffmpeg

EPS = 1e-12


@dataclass
class CutConfig:
    """Silence/VAD detection and timeline export configuration."""

    silence_thresh_db: float = -40.0
    min_silence_ms: float = 400.0
    min_speech_ms: float = 150.0
    pad_ms: float = 80.0
    frame_ms: float = 20.0
    hop_ms: float = 10.0
    zcr_unvoiced_threshold: float = 0.25
    zcr_margin_db: float = 6.0
    fade_ms: float = 5.0
    fps: float = 25.0


@dataclass
class SpeechSegment:
    """Detected active speech segment with timing and acoustic summary."""

    index: int
    start_s: float
    end_s: float
    duration_s: float
    rms_dbfs: float
    mean_zcr: float
    text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _compute_frame_features(
    mono: np.ndarray,
    fs: int,
    frame_ms: float,
    hop_ms: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute per-frame center time (s), RMS (dBFS), and zero-crossing rate."""
    frame_len = max(8, int(round(frame_ms * 1e-3 * fs)))
    hop_len = max(1, int(round(hop_ms * 1e-3 * fs)))

    if len(mono) < frame_len:
        padded = np.pad(mono, (0, frame_len - len(mono)))
        frames = padded[np.newaxis, :]
    else:
        n_frames = 1 + (len(mono) - frame_len) // hop_len
        indices = np.arange(frame_len)[None, :] + np.arange(n_frames)[:, None] * hop_len
        frames = mono[indices]

    rms = np.sqrt(np.mean(frames * frames, axis=1))
    rms_db = np.maximum(-120.0, 20.0 * np.log10(np.maximum(rms, EPS)))

    signs = frames >= 0.0
    zcr = np.mean(np.abs(np.diff(signs.astype(np.int8), axis=1)), axis=1)
    times = (np.arange(frames.shape[0]) * hop_len + 0.5 * frame_len) / fs
    return times, rms_db, zcr


def detect_speech_segments(
    audio: AudioBuffer,
    config: CutConfig | None = None,
) -> list[SpeechSegment]:
    """Detect non-silent segments using short-time RMS energy and ZCR hysteresis."""
    cfg = config or CutConfig()
    if audio.n_samples == 0:
        return []

    fs = audio.sample_rate
    mono = audio.to_mono()
    mono = mono - np.mean(mono)
    total_dur = audio.duration_s

    times, rms_db, zcr = _compute_frame_features(mono, fs, cfg.frame_ms, cfg.hop_ms)
    hop_s = cfg.hop_ms * 1e-3

    # Primary energy threshold + ZCR extension for low-energy unvoiced fricatives.
    active = (rms_db >= cfg.silence_thresh_db) | (
        (rms_db >= cfg.silence_thresh_db - cfg.zcr_margin_db)
        & (zcr >= cfg.zcr_unvoiced_threshold)
    )

    if not np.any(active):
        return []

    # Bridge short silent gaps (< min_silence_ms) inside speech.
    min_silence_frames = max(1, int(np.floor(cfg.min_silence_ms * 1e-3 / hop_s)))
    bridged = active.copy()
    i = 0
    while i < len(bridged):
        if not bridged[i]:
            j = i
            while j < len(bridged) and not bridged[j]:
                j += 1
            if i > 0 and j < len(bridged) and (j - i) < min_silence_frames:
                bridged[i:j] = True
            i = j
        else:
            i += 1

    # Extract contiguous active runs and drop islands shorter than min_speech_ms.
    min_speech_s = cfg.min_speech_ms * 1e-3
    pad_s = cfg.pad_ms * 1e-3
    half_frame_s = 0.5 * cfg.frame_ms * 1e-3

    raw_intervals: list[tuple[float, float]] = []
    i = 0
    while i < len(bridged):
        if bridged[i]:
            j = i
            while j < len(bridged) and bridged[j]:
                j += 1
            start_s = max(0.0, times[i] - half_frame_s)
            end_s = min(total_dur, times[j - 1] + half_frame_s)
            if (end_s - start_s) >= min_speech_s:
                raw_intervals.append((max(0.0, start_s - pad_s), min(total_dur, end_s + pad_s)))
            i = j
        else:
            i += 1

    if not raw_intervals:
        return []

    # Merge intervals that overlap after padding.
    merged: list[tuple[float, float]] = [raw_intervals[0]]
    for start_s, end_s in raw_intervals[1:]:
        prev_start, prev_end = merged[-1]
        if start_s <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end_s))
        else:
            merged.append((start_s, end_s))

    segments: list[SpeechSegment] = []
    for idx, (start_s, end_s) in enumerate(merged, start=1):
        s0 = max(0, int(round(start_s * fs)))
        s1 = min(audio.n_samples, int(round(end_s * fs)))
        seg_mono = mono[s0:s1]
        seg_rms = float(np.sqrt(np.mean(seg_mono * seg_mono))) if len(seg_mono) > 0 else 0.0
        seg_rms_db = float(max(-120.0, 20.0 * np.log10(max(seg_rms, EPS))))
        seg_zcr = (
            float(np.mean(np.abs(np.diff((seg_mono >= 0.0).astype(np.int8)))))
            if len(seg_mono) > 1
            else 0.0
        )
        segments.append(
            SpeechSegment(
                index=idx,
                start_s=round(start_s, 6),
                end_s=round(end_s, 6),
                duration_s=round(end_s - start_s, 6),
                rms_dbfs=round(seg_rms_db, 3),
                mean_zcr=round(seg_zcr, 4),
            )
        )
    return segments


def export_otio(
    segments: list[SpeechSegment],
    source_path: str | Path,
    total_duration_s: float,
    fps: float = 25.0,
    timeline_name: str = "loudgate_cut",
) -> dict[str, Any]:
    """Generate a clean-room OpenTimelineIO (.otio) JSON timeline structure."""
    src_uri = Path(source_path).resolve().as_uri() if Path(source_path).exists() else str(source_path)
    total_frames = max(1, int(round(total_duration_s * fps)))

    media_ref = {
        "OTIO_SCHEMA": "ExternalReference.1",
        "metadata": {},
        "name": Path(source_path).name,
        "available_range": {
            "OTIO_SCHEMA": "TimeRange.1",
            "duration": {"OTIO_SCHEMA": "RationalTime.1", "rate": fps, "value": total_frames},
            "start_time": {"OTIO_SCHEMA": "RationalTime.1", "rate": fps, "value": 0},
        },
        "target_url": src_uri,
    }

    children: list[dict[str, Any]] = []
    for seg in segments:
        start_frames = int(round(seg.start_s * fps))
        dur_frames = max(1, int(round(seg.duration_s * fps)))
        children.append(
            {
                "OTIO_SCHEMA": "Clip.2",
                "metadata": {"rms_dbfs": seg.rms_dbfs, "mean_zcr": seg.mean_zcr},
                "name": f"seg_{seg.index:03d}",
                "source_range": {
                    "OTIO_SCHEMA": "TimeRange.1",
                    "duration": {"OTIO_SCHEMA": "RationalTime.1", "rate": fps, "value": dur_frames},
                    "start_time": {"OTIO_SCHEMA": "RationalTime.1", "rate": fps, "value": start_frames},
                },
                "effects": [],
                "markers": [],
                "media_references": {"DEFAULT_MEDIA": media_ref},
                "active_media_reference_key": "DEFAULT_MEDIA",
            }
        )

    return {
        "OTIO_SCHEMA": "Timeline.1",
        "metadata": {"generator": "loudgate"},
        "name": timeline_name,
        "global_start_time": {"OTIO_SCHEMA": "RationalTime.1", "rate": fps, "value": 0},
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "metadata": {},
            "name": "tracks",
            "children": [
                {
                    "OTIO_SCHEMA": "Track.1",
                    "metadata": {},
                    "name": "A1",
                    "kind": "Audio",
                    "children": children,
                    "effects": [],
                    "markers": [],
                }
            ],
            "effects": [],
            "markers": [],
        },
    }


def _seconds_to_smpte(seconds: float, fps: float) -> str:
    total_frames = max(0, int(round(seconds * fps)))
    fps_int = max(1, int(round(fps)))
    ff = total_frames % fps_int
    total_sec = total_frames // fps_int
    ss = total_sec % 60
    mm = (total_sec // 60) % 60
    hh = total_sec // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def export_edl(
    segments: list[SpeechSegment],
    source_path: str | Path,
    fps: float = 25.0,
    title: str = "LOUDGATE_CUT",
) -> str:
    """Generate a CMX 3600 EDL for audio cut segments."""
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    rec_cursor_s = 0.0
    reel = Path(source_path).stem[:8].upper() or "AX"

    for idx, seg in enumerate(segments, start=1):
        src_in = _seconds_to_smpte(seg.start_s, fps)
        src_out = _seconds_to_smpte(seg.end_s, fps)
        rec_in = _seconds_to_smpte(rec_cursor_s, fps)
        rec_cursor_s += seg.duration_s
        rec_out = _seconds_to_smpte(rec_cursor_s, fps)
        lines.append(f"{idx:03d}  {reel:<8} A     C        {src_in} {src_out} {rec_in} {rec_out}")
        lines.append(f"* FROM CLIP NAME: {Path(source_path).name}")
    lines.append("")
    return "\n".join(lines)


def _format_ts_srt(seconds: float) -> str:
    ms_total = max(0, int(round(seconds * 1000.0)))
    ms = ms_total % 1000
    sec_total = ms_total // 1000
    ss = sec_total % 60
    mm = (sec_total // 60) % 60
    hh = sec_total // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def _format_ts_vtt(seconds: float) -> str:
    return _format_ts_srt(seconds).replace(",", ".")


def _cue_ranges(segments: list[SpeechSegment], use_kept_timeline: bool) -> list[tuple[float, float, str]]:
    cues: list[tuple[float, float, str]] = []
    cursor = 0.0
    for seg in segments:
        label = seg.text or f"[Segment {seg.index:02d}]"
        if use_kept_timeline:
            start = cursor
            end = cursor + seg.duration_s
            cursor = end
        else:
            start = seg.start_s
            end = seg.end_s
        cues.append((start, end, label))
    return cues


def export_srt(segments: list[SpeechSegment], use_kept_timeline: bool = True) -> str:
    """Generate SubRip (.srt) subtitle file from active speech segments."""
    blocks: list[str] = []
    for idx, (start, end, label) in enumerate(_cue_ranges(segments, use_kept_timeline), start=1):
        blocks.append(f"{idx}\n{_format_ts_srt(start)} --> {_format_ts_srt(end)}\n{label}\n")
    return "\n".join(blocks)


def export_vtt(segments: list[SpeechSegment], use_kept_timeline: bool = True) -> str:
    """Generate WebVTT (.vtt) subtitle file from active speech segments."""
    lines = ["WEBVTT", ""]
    for start, end, label in _cue_ranges(segments, use_kept_timeline):
        lines.append(f"{_format_ts_vtt(start)} --> {_format_ts_vtt(end)}")
        lines.append(label)
        lines.append("")
    return "\n".join(lines)


def build_ffmpeg_trim_filtergraph(
    segments: list[SpeechSegment],
    fade_ms: float = 5.0,
) -> str:
    """Build deterministic FFmpeg atrim + micro-fade + concat filter_complex string."""
    if not segments:
        raise ValueError("Cannot build trim filtergraph from empty segment list.")

    fade_s = max(0.0, fade_ms * 1e-3)
    chains: list[str] = []
    labels: list[str] = []

    for i, seg in enumerate(segments):
        label = f"s{i}"
        labels.append(f"[{label}]")
        dur = max(0.001, seg.end_s - seg.start_s)
        f_dur = min(fade_s, 0.25 * dur)
        fade_clause = ""
        if f_dur >= 0.0005:
            out_st = max(0.0, dur - f_dur)
            fade_clause = f",afade=t=in:st=0:d={f_dur:.6f},afade=t=out:st={out_st:.6f}:d={f_dur:.6f}"
        chains.append(
            f"[0:a]atrim=start={seg.start_s:.6f}:end={seg.end_s:.6f},"
            f"asetpts=PTS-STARTPTS{fade_clause}[{label}]"
        )

    if len(segments) == 1:
        chains[-1] = chains[-1].replace("[s0]", "[outa]")
    else:
        chains.append(f"{''.join(labels)}concat=n={len(segments)}:v=0:a=1[outa]")
    return ";".join(chains)


def render_cut_audio(
    input_path: str | Path,
    output_path: str | Path,
    segments: list[SpeechSegment],
    fade_ms: float = 5.0,
) -> Path:
    """Render trimmed audio via FFmpeg atrim/concat filtergraph."""
    dst = Path(output_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    filtergraph = build_ffmpeg_trim_filtergraph(segments, fade_ms=fade_ms)

    ext = dst.suffix.lower()
    codec_args = ["-c:a", "pcm_s24le"]
    if ext == ".mp3":
        codec_args = ["-c:a", "libmp3lame", "-b:a", "192k"]
    elif ext in {".aac", ".m4a"}:
        codec_args = ["-c:a", "aac", "-b:a", "192k"]
    elif ext == ".flac":
        codec_args = ["-c:a", "flac"]

    run_ffmpeg(
        [
            "-y",
            "-i",
            str(input_path),
            "-filter_complex",
            filtergraph,
            "-map",
            "[outa]",
            *codec_args,
            str(dst),
        ]
    )
    return dst


def cut_silences(
    input_path: str | Path,
    output_dir: str | Path,
    config: CutConfig | None = None,
    render_audio: bool = True,
    use_kept_timeline_subtitles: bool = True,
) -> dict[str, Any]:
    """Detect speech segments and write OTIO, EDL, SRT, VTT, and optional trimmed WAV."""
    cfg = config or CutConfig()
    src = Path(input_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    audio = read_audio(src)
    segments = detect_speech_segments(audio, config=cfg)
    stem = src.stem

    otio_path = out_dir / f"{stem}.otio"
    edl_path = out_dir / f"{stem}.edl"
    srt_path = out_dir / f"{stem}.srt"
    vtt_path = out_dir / f"{stem}.vtt"

    otio_data = export_otio(segments, source_path=src, total_duration_s=audio.duration_s, fps=cfg.fps)
    otio_path.write_text(json.dumps(otio_data, indent=2), encoding="utf-8")
    edl_path.write_text(export_edl(segments, source_path=src, fps=cfg.fps), encoding="utf-8")
    srt_path.write_text(export_srt(segments, use_kept_timeline=use_kept_timeline_subtitles), encoding="utf-8")
    vtt_path.write_text(export_vtt(segments, use_kept_timeline=use_kept_timeline_subtitles), encoding="utf-8")

    trimmed_audio_path: str | None = None
    if render_audio and segments:
        wav_out = out_dir / f"{stem}_cut.wav"
        render_cut_audio(src, wav_out, segments, fade_ms=cfg.fade_ms)
        trimmed_audio_path = str(wav_out)

    kept_duration_s = round(sum(s.duration_s for s in segments), 6)
    return {
        "input_path": str(src),
        "input_duration_s": round(audio.duration_s, 6),
        "kept_duration_s": kept_duration_s,
        "removed_duration_s": round(max(0.0, audio.duration_s - kept_duration_s), 6),
        "segment_count": len(segments),
        "segments": [s.to_dict() for s in segments],
        "otio_path": str(otio_path),
        "edl_path": str(edl_path),
        "srt_path": str(srt_path),
        "vtt_path": str(vtt_path),
        "trimmed_audio_path": trimmed_audio_path,
    }
