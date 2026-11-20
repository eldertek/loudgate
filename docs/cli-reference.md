# CLI & Python API Reference

[Reference Francaise (FR)](fr/cli-reference.md) | [Documentation Index](index.md)

## Global CLI Structure

```bash
loudgate <command> [options]
loudgate-ui [options]
```

Available commands:
- `loudgate probe` - EBU R128 loudness, peak, DC offset, clipping, and SNR diagnostics
- `loudgate clean` - Zero-phase HPF, DC removal, MAD de-clicker, STFT Wiener denoising, de-esser, gate
- `loudgate master` - Two-pass linear FFmpeg `loudnorm` + 4x oversampled true-peak safety limiter
- `loudgate cut` - RMS/ZCR VAD silence detection and OTIO/EDL/SRT/VTT/WAV export
- `loudgate ui` (or `loudgate-ui`) - Local loopback web Studio GUI
- `loudgate mcp` - JSON-RPC 2.0 `stdio` Model Context Protocol server

---

## `loudgate probe`

```bash
loudgate probe <input> [--clip-threshold 0.999] [--json]
```

| Flag | Default | Description |
| :--- | :--- | :--- |
| `input` | required | Path to input audio file (WAV, FLAC, OGG, AIFF, MP3, M4A, AAC, OPUS). |
| `--clip-threshold` | `0.999` | Absolute sample amplitude threshold used to count clipped samples. |
| `--json` | `false` | Emit machine-readable `ProbeReport` JSON. |

Example JSON output fields: `integrated_lufs`, `threshold_lufs`, `lra_lu`, `lra_low_lufs`, `lra_high_lufs`, `ffmpeg_sample_peak_dbfs`, `true_peak_dbtp`, `sample_peak_dbfs`, `oversampled_true_peak_dbtp`, `inter_sample_peak_excess_db`, `has_inter_sample_clipping`, `rms_dbfs`, `crest_factor_db`, `dc_offset_per_channel`, `max_abs_dc_offset`, `clipped_samples`, `clipped_ratio`, `has_clipping`, `noise_floor_dbfs`, `signal_level_dbfs`, `estimated_snr_db`.

---

## `loudgate clean`

```bash
loudgate clean <input> <output> [options]
```

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--highpass-hz` | `80.0` | Zero-phase 4th-order Butterworth HPF cutoff (`0` disables HPF). |
| `--denoise-strength` | `1.0` | Wiener over-subtraction factor (`0.0` bypasses denoising). |
| `--spectral-floor-db` | `-28.0` | Minimum spectral gain floor in dB to avoid musical noise artifacts. |
| `--n-fft` | `2048` | STFT window / FFT size (must be integer multiple `>= 2` of `--hop-length`). |
| `--hop-length` | `512` | STFT frame hop in samples. |
| `--no-declick` | `false` | Disable MAD second-difference impulse click repair. |
| `--deess` | `false` | Enable split-band sibilance attenuator (`5000` to `10000 Hz`). |
| `--gate` | `false` | Enable smooth downward noise gate (`-45 dBFS` threshold, `-24 dB` range). |
| `--json` | `false` | Emit `CleanStats` JSON (`clicks_repaired`, `parseval_relative_error`, `noise_reduction_db_est`). |

---

## `loudgate master`

```bash
loudgate master <input> <output> [options]
```

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--preset` | `podcast` | Target preset from `MASTER_PRESETS`. |
| `--target-lufs` | `None` | Override preset target integrated loudness (`LUFS`). |
| `--max-tp` | `-1.0` | Maximum allowed true peak ceiling (`dBTP`). |
| `--bitrate` | `192k` | Bitrate for lossy exports (`.mp3`, `.aac`, `.m4a`, `.opus`). |
| `--dual-mono` | `false` | Pass `dual_mono=true` to FFmpeg `loudnorm`. |
| `--json` | `false` | Emit `MasterResult` JSON including `pass1_stats`, `pass2_stats`, and `output_probe`. |

### Mastering Presets (`MASTER_PRESETS`)

| Preset | Target Integrated Loudness | Max True Peak | Target LRA | Typical Use |
| :--- | :--- | :--- | :--- | :--- |
| `podcast` | `-14.0 LUFS` | `-1.0 dBTP` | `11.0 LU` | Podcasts and spoken-word shows |
| `web` | `-14.0 LUFS` | `-1.0 dBTP` | `11.0 LU` | YouTube and web streaming |
| `mobile` | `-16.0 LUFS` | `-1.0 dBTP` | `9.0 LU` | Mobile playback and voice apps |
| `stereo` | `-16.0 LUFS` | `-1.0 dBTP` | `9.0 LU` | Apple Podcasts / stereo speech |
| `broadcast` | `-23.0 LUFS` | `-1.0 dBTP` | `7.0 LU` | European broadcast delivery |
| `ebu_r128` | `-23.0 LUFS` | `-1.0 dBTP` | `7.0 LU` | Strict EBU R128 compliance |

---

## `loudgate cut`

```bash
loudgate cut <input> <output_dir> [options]
```

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--thresh-db` | `-40.0` | Primary RMS energy speech threshold (`dBFS`). |
| `--min-silence-ms` | `400.0` | Minimum silence gap required to split segments (ms). |
| `--min-speech-ms` | `150.0` | Minimum active speech duration kept (ms). |
| `--pad-ms` | `80.0` | Pre/post speech padding added around each segment (ms). |
| `--fps` | `25.0` | Frame rate used in OpenTimelineIO and CMX 3600 EDL timecodes. |
| `--no-render` | `false` | Export `.otio`, `.edl`, `.srt`, `.vtt` only without rendering `_cut.wav`. |
| `--json` | `false` | Emit segment list and output paths as JSON. |

---

## `loudgate ui` / `loudgate-ui`

```bash
loudgate ui [--host 127.0.0.1] [--port 8742] [--workspace-dir .] [--no-open] [--desktop]
loudgate-ui [--host 127.0.0.1] [--port 8742] [--workspace-dir .] [--no-open] [--desktop]
```

---

## Python Library API

```python
from loudgate import (
    CleanConfig,
    CutConfig,
    MASTER_PRESETS,
    MasterConfig,
    clean_file,
    cut_silences,
    master_file,
    probe_file,
)

report = probe_file("raw_episode.wav")
stats = clean_file("raw_episode.wav", "clean.wav", CleanConfig(highpass_hz=80.0, deess_enabled=True))
master = master_file("clean.wav", "master.wav", MasterConfig(preset="podcast"))
cuts = cut_silences("master.wav", "exports/", CutConfig(silence_thresh_db=-40.0, fps=25.0))
```
