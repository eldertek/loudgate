# loudgate

Open-source, clean-room broadcast audio post-production engine in Python + FFmpeg.

`loudgate` gives audio editors, podcast producers, and post-production pipelines deterministic tools for loudness auditing, STFT spectral cleanup, EBU R128 mastering, silence cutting with timeline/subtitle export, and MCP integration. No cloud upload, no GPU weight downloads, and no subscription paywalls.

## Why loudgate

| Workflow | Proprietary paywalls | loudgate (MIT) |
| :--- | :--- | :--- |
| Spectral denoise, de-click, HPF | Desktop GUI suites ($399+) | Deterministic STFT WOLA + zero-phase Butterworth SOS |
| Loudness audit & mastering | Monthly NLE add-ons ($22.99/mo) | Two-pass FFmpeg `loudnorm` + 4x oversampled true-peak ceiling |
| Silence removal + NLE cut export | Per-seat podcast editors ($24-$65/mo) | Energy/ZCR VAD + `.otio` / CMX 3600 `.edl` / `.srt` / `.vtt` / WAV |
| Automation & editor integration | Closed plugins | Scriptable CLI, Python API, and stdio MCP server |

## Requirements

- Python 3.10+
- FFmpeg and FFprobe available on `PATH`
- Python packages: `numpy`, `scipy`, `soundfile`

## Installation

```bash
git clone https://github.com/eldertek/loudgate.git
cd loudgate
pip install -e ".[dev]"
```

## Quick Start (CLI)

### 1. Audit loudness, peaks, clipping, DC offset, and SNR

```bash
loudgate probe episode_raw.wav --json
```

Reports EBU R128 integrated loudness (`LUFS`), loudness range (`LRA`), FFmpeg true peak (`dBTP`), 4x polyphase oversampled inter-sample peak excess, DC offset per channel, clipped sample ratio, and estimated SNR (`dB`).

### 2. Clean noise, rumble, and clicks

```bash
loudgate clean episode_raw.wav episode_clean.wav --highpass-hz 80 --denoise-strength 1.0 --deess
```

Pipeline stages:
- DC offset removal
- Zero-phase 4th-order Butterworth high-pass filter (`scipy.signal.sosfiltfilt`, zero group-delay distortion)
- MAD second-difference de-clicker for isolated digital impulses
- Tight-frame periodic root-Hann STFT WOLA spectral Wiener denoising (`n_fft=2048`, `hop_length=512`)
- Optional split-band sibilance attenuator (`--deess`) and downward noise gate (`--gate`)

### 3. Master to broadcast or podcast targets

```bash
# Podcast / Web (-14 LUFS, <= -1.0 dBTP)
loudgate master episode_clean.wav episode_podcast.wav --preset podcast

# EBU R128 Broadcast (-23 LUFS, <= -1.0 dBTP)
loudgate master episode_clean.wav episode_broadcast.wav --preset broadcast

# Mobile / Stereo (-16 LUFS, <= -1.0 dBTP) + MP3 export
loudgate master episode_clean.wav episode_mobile.mp3 --preset mobile --bitrate 192k
```

Uses two-pass linear FFmpeg `loudnorm` followed by a post-render `ebur128` verification pass and a 4x oversampled PCM true-peak safety limiter when needed.

### 4. Cut silences and export NLE timelines + subtitles

```bash
loudgate cut episode_podcast.wav ./cuts/ --thresh-db -40 --min-silence-ms 400 --pad-ms 80 --fps 25
```

Outputs into `./cuts/`:
- `episode_podcast.otio` (OpenTimelineIO JSON timeline ready for DaVinci Resolve / Premiere workflows)
- `episode_podcast.edl` (CMX 3600 EDL)
- `episode_podcast.srt` and `episode_podcast.vtt` (cue timestamps aligned to the trimmed timeline)
- `episode_podcast_cut.wav` (rendered via FFmpeg `atrim` + micro-fades + `concat`)

### 5. Run as an MCP stdio server

```bash
loudgate mcp
```

Exposed MCP tools:
- `probe_loudness`
- `clean_spectral`
- `master_broadcast`
- `cut_silences_otio`

Example MCP client configuration:

```json
{
  "mcpServers": {
    "loudgate": {
      "command": "loudgate",
      "args": ["mcp"]
    }
  }
}
```

## Python API

```python
from loudgate import CleanConfig, MasterConfig, clean_file, cut_silences, master_file, probe_file

report = probe_file("raw.wav")
clean_file("raw.wav", "clean.wav", CleanConfig(highpass_hz=80.0, denoise_strength=1.0))
master = master_file("clean.wav", "master.wav", MasterConfig(preset="podcast"))
cuts = cut_silences("master.wav", "exports/")
```

## Running Tests

```bash
pytest -v
```

The test suite verifies DSP invariants on synthetic signals (pure sine waves, logarithmic chirps, AWGN noise, DC offsets, clipped bursts, and inter-sample peaks):
- Parseval energy conservation across the tight-frame STFT/ISTFT (`relative error < 1e-12`)
- Zero-phase high-pass attenuation and impulse click repair
- Real FFmpeg `ebur128` verification before and after two-pass `loudnorm` at `-14`, `-16`, and `-23 LUFS` with `true_peak_dbtp <= -1.0`
- VAD/silence segmentation accuracy, valid OTIO/EDL/SRT/VTT generation, and FFmpeg `atrim` rendering
- MCP JSON-RPC 2.0 stdio protocol round-trips

## License

MIT License. See [LICENSE](LICENSE).
