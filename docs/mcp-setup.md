# MCP Server Setup Guide (`loudgate mcp`)

[Guide Francais (FR)](fr/mcp-setup.md) | [Documentation Index](index.md)

`loudgate` includes a clean-room JSON-RPC 2.0 `stdio` Model Context Protocol (MCP) server (`src/loudgate/mcp_server.py`, protocol version `2024-11-05`) that exposes deterministic audio post-production tools to AI coding assistants and local agent workflows.

## Starting the Server Manually

```bash
loudgate mcp
# Or via Python module invocation:
python -m loudgate mcp
```

## Client Configurations

### Claude Desktop (`claude_desktop_config.json`)

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

If `loudgate` is installed inside a virtual environment, specify the absolute path to the interpreter or executable:

```json
{
  "mcpServers": {
    "loudgate": {
      "command": "C:/path/to/venv/Scripts/python.exe",
      "args": ["-m", "loudgate", "mcp"]
    }
  }
}
```

### Cursor / VS Code / Windsurf MCP Config

Add the server entry to your workspace `.cursor/mcp.json` or `.vscode/mcp.json`:

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

## Exposed MCP Tools

### 1. `probe_loudness`

Audit an audio file for EBU R128 loudness (`LUFS`), `LRA` (`LU`), true peak (`dBTP`), 4x oversampled inter-sample peaks, DC offset, clipping, and SNR.

- `path` (string, required): Input audio file path.
- `clip_threshold` (number, default `0.999`): Amplitude threshold for sample clipping detection.

### 2. `clean_spectral`

Run deterministic DSP cleanup: DC removal, zero-phase Butterworth high-pass, MAD de-clicker, STFT WOLA spectral denoising, optional de-esser and noise gate.

- `input_path` (string, required)
- `output_path` (string, required)
- `highpass_hz` (number, default `80.0`)
- `denoise_strength` (number, default `1.0`)
- `spectral_floor_db` (number, default `-28.0`)
- `declick` (boolean, default `true`)
- `deess_enabled` (boolean, default `false`)
- `gate_enabled` (boolean, default `false`)
- `n_fft` (integer, default `2048`)
- `hop_length` (integer, default `512`)

### 3. `master_broadcast`

Normalize audio to broadcast/podcast loudness targets with two-pass FFmpeg `loudnorm` and true-peak safety ceiling (`<= -1.0 dBTP`).

- `input_path` (string, required)
- `output_path` (string, required)
- `preset` (string enum: `web`, `podcast`, `mobile`, `stereo`, `broadcast`, `ebu_r128`; default `podcast`)
- `target_lufs` (number or null, default `null`)
- `max_true_peak_dbtp` (number, default `-1.0`)
- `bitrate` (string, default `192k`)

### 4. `cut_silences_otio`

Detect silence vs active speech (`RMS` energy + `ZCR`) and export OpenTimelineIO (`.otio`), CMX 3600 EDL (`.edl`), SubRip (`.srt`), WebVTT (`.vtt`), and optional trimmed WAV.

- `input_path` (string, required)
- `output_dir` (string, required)
- `silence_thresh_db` (number, default `-40.0`)
- `min_silence_ms` (number, default `400.0`)
- `min_speech_ms` (number, default `150.0`)
- `pad_ms` (number, default `80.0`)
- `fps` (number, default `25.0`)
- `render_audio` (boolean, default `true`)

## Troubleshooting

- **`ffmpeg` / `ffprobe` not found**: Ensure FFmpeg is installed on the system `PATH` inherited by the MCP client process.
- **Line-delimited JSON-RPC**: `loudgate mcp` reads newline-delimited JSON-RPC 2.0 objects from `stdin` and writes responses to `stdout`. Never print debug logs to `stdout` when wrapping it.
