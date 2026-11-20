"""Silence/VAD detection, OTIO/EDL/SRT/VTT export, and FFmpeg trim tests."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from loudgate.audio_io import AudioBuffer, read_audio, write_audio
from loudgate.cut import CutConfig, cut_silences, detect_speech_segments


def test_cut_silences_detects_segments_and_exports_timelines(tmp_path: Path) -> None:
    rng = np.random.default_rng(2026)
    fs = 48000
    duration_s = 5.0
    n = int(fs * duration_s)
    t = np.arange(n, dtype=np.float64) / fs

    # Low -65 dBFS room noise floor + three 0.8s speech-like bursts
    y = 0.0005 * rng.standard_normal(n)
    bursts = [(0.3, 1.1), (1.9, 2.7), (3.6, 4.4)]
    for s0_t, s1_t in bursts:
        i0, i1 = int(s0_t * fs), int(s1_t * fs)
        y[i0:i1] += 0.25 * np.sin(2.0 * np.pi * 440.0 * t[i0:i1])

    src = tmp_path / "session_sample.wav"
    out_dir = tmp_path / "cut_out"
    write_audio(src, AudioBuffer(samples=y, sample_rate=fs), subtype="PCM_24")

    cfg = CutConfig(
        silence_thresh_db=-36.0,
        min_silence_ms=350.0,
        min_speech_ms=150.0,
        pad_ms=60.0,
        fps=25.0,
    )
    segs = detect_speech_segments(read_audio(src), config=cfg)
    assert len(segs) == 3
    for seg, (exp_start, exp_end) in zip(segs, bursts):
        assert abs(seg.start_s - (exp_start - 0.06)) < 0.04
        assert abs(seg.end_s - (exp_end + 0.06)) < 0.04

    res = cut_silences(src, out_dir, config=cfg, render_audio=True)
    assert res["segment_count"] == 3
    assert res["removed_duration_s"] > 1.8

    otio = json.loads(Path(res["otio_path"]).read_text(encoding="utf-8"))
    assert otio["OTIO_SCHEMA"] == "Timeline.1"
    clips = otio["tracks"]["children"][0]["children"]
    assert len(clips) == 3
    assert clips[0]["OTIO_SCHEMA"] == "Clip.2"

    edl_text = Path(res["edl_path"]).read_text(encoding="utf-8")
    assert "TITLE: LOUDGATE_CUT" in edl_text
    assert "FCM: NON-DROP FRAME" in edl_text
    assert "003  " in edl_text

    srt_text = Path(res["srt_path"]).read_text(encoding="utf-8")
    vtt_text = Path(res["vtt_path"]).read_text(encoding="utf-8")
    assert "00:00:00,000 -->" in srt_text
    assert vtt_text.startswith("WEBVTT")

    trimmed_wav = Path(res["trimmed_audio_path"])
    assert trimmed_wav.exists()
    trimmed_buf = read_audio(trimmed_wav)
    assert abs(trimmed_buf.duration_s - res["kept_duration_s"]) < 0.05
