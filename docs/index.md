# loudgate Documentation

[Francais (FR)](fr/index.md) | **English (EN)**

`loudgate` is an open-source, clean-room broadcast audio post-production engine built on Python, NumPy/SciPy, soundfile, and local FFmpeg. Everything runs deterministically on your CPU with zero cloud uploads, zero telemetry, and zero subscription paywalls.

## Documentation Guides

| Guide | What it covers |
| :--- | :--- |
| [Local Studio GUI Guide](gui-guide.md) | Launch `loudgate ui` / `loudgate-ui`, load audio or the built-in sample, inspect EBU R128 meters, run STFT denoising, master to broadcast targets, cut silences, and export timelines. |
| [CLI & Python API Reference](cli-reference.md) | Complete reference for `probe`, `clean`, `master`, `cut`, `ui`, `mcp`, presets (`MASTER_PRESETS`), flags, JSON schemas, and Python library calls. |
| [MCP Server Setup Guide](mcp-setup.md) | Connect `loudgate mcp` (`stdio` JSON-RPC 2.0) to Claude Desktop, Cursor, VS Code, or custom AI workflows. |

## Core Post-Production Workflow

1. **Audit (`probe`)**: Measure EBU R128 integrated loudness (`LUFS`), loudness range (`LRA`), FFmpeg true peak (`dBTP`), 4x polyphase oversampled inter-sample peak excess, DC offset, clipped sample count, and estimated SNR (`dB`).
2. **Cleanup (`clean`)**: Remove DC offset, apply zero-phase 4th-order Butterworth high-pass filtering (`scipy.signal.sosfiltfilt`), repair isolated digital clicks via MAD second-difference detection, run tight-frame periodic root-Hann STFT WOLA Wiener denoising, and optionally apply split-band de-essing and downward noise gating.
3. **Master (`master`)**: Normalize with two-pass linear FFmpeg `loudnorm`, verify post-render `ebur128`, and apply a 4x oversampled lookahead PCM safety limiter whenever true peak exceeds ceiling.
4. **Silence Cut & NLE Export (`cut`)**: Segment speech vs silence using 20 ms RMS energy and zero-crossing rate (ZCR) hysteresis, then export OpenTimelineIO (`.otio`), CMX 3600 (`.edl`), SubRip (`.srt`), WebVTT (`.vtt`), and FFmpeg `atrim` + `concat` trimmed audio (`.wav`).

## System Requirements

- Python 3.10 or newer
- `ffmpeg` and `ffprobe` installed and available on `PATH`
- Python runtime dependencies: `numpy>=1.24`, `scipy>=1.11`, `soundfile>=0.12`

## Quick Installation

```bash
git clone https://github.com/eldertek/loudgate.git
cd loudgate
pip install -e ".[dev]"
```

Launch the local Studio GUI immediately:

```bash
loudgate ui
```
