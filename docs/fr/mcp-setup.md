# Guide Configuration Serveur MCP (`loudgate mcp`)

[English Guide (EN)](../mcp-setup.md) | [Index Documentation](index.md)

`loudgate` integre un serveur Model Context Protocol (MCP) JSON-RPC 2.0 sur `stdio` (`src/loudgate/mcp_server.py`, version protocole `2024-11-05`) exposant les outils de post-production audio deterministes aux assistants IA et pipelines locaux.

## Lancement manuel

```bash
loudgate mcp
# Ou via module Python :
python -m loudgate mcp
```

## Configuration des clients MCP

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

Si `loudgate` est installe dans un environnement virtuel, indiquez le chemin absolu de l'interpreteur Python :

```json
{
  "mcpServers": {
    "loudgate": {
      "command": "C:/chemin/vers/venv/Scripts/python.exe",
      "args": ["-m", "loudgate", "mcp"]
    }
  }
}
```

### Cursor / VS Code / Windsurf

Ajoutez l'entree dans `.cursor/mcp.json` ou `.vscode/mcp.json` :

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

## Outils MCP exposes

### 1. `probe_loudness`

Audite un fichier audio (LUFS EBU R128, LRA, crete vraie dBTP, crete surechantillonnee 4x, offset DC, clipping et RSB).

- `path` (chaine, requis) : Chemin du fichier audio d'entree.
- `clip_threshold` (nombre, defaut `0.999`) : Seuil d'amplitude pour detecter les echantillons satures.

### 2. `clean_spectral`

Execute le nettoyage DSP deterministe : suppression DC, passe-haut Butterworth phase nulle, de-clicker MAD, debruitage spectral STFT WOLA, de-esseur et noise gate optionnels.

- `input_path` (chaine, requis)
- `output_path` (chaine, requis)
- `highpass_hz` (nombre, defaut `80.0`)
- `denoise_strength` (nombre, defaut `1.0`)
- `spectral_floor_db` (nombre, defaut `-28.0`)
- `declick` (booleen, defaut `true`)
- `deess_enabled` (booleen, defaut `false`)
- `gate_enabled` (booleen, defaut `false`)
- `n_fft` (entier, defaut `2048`)
- `hop_length` (entier, defaut `512`)

### 3. `master_broadcast`

Normalise l'audio aux cibles broadcast/podcast avec FFmpeg `loudnorm` deux passes et plafond crete vraie `<= -1.0 dBTP`.

- `input_path` (chaine, requis)
- `output_path` (chaine, requis)
- `preset` (`web`, `podcast`, `mobile`, `stereo`, `broadcast`, `ebu_r128`; defaut `podcast`)
- `target_lufs` (nombre ou null, defaut `null`)
- `max_true_peak_dbtp` (nombre, defaut `-1.0`)
- `bitrate` (chaine, defaut `192k`)

### 4. `cut_silences_otio`

Detecte la parole active vs les silences (`RMS` + `ZCR`) et exporte `.otio`, `.edl`, `.srt`, `.vtt` et le `.wav` decoupe optionnel.

- `input_path` (chaine, requis)
- `output_dir` (chaine, requis)
- `silence_thresh_db` (nombre, defaut `-40.0`)
- `min_silence_ms` (nombre, defaut `400.0`)
- `min_speech_ms` (nombre, defaut `150.0`)
- `pad_ms` (nombre, defaut `80.0`)
- `fps` (nombre, defaut `25.0`)
- `render_audio` (booleen, defaut `true`)

## Depannage

- **`ffmpeg` / `ffprobe` introuvable** : Verifiez que FFmpeg est present dans le `PATH` systeme herite par le client MCP.
- **JSON-RPC delimite par ligne** : `loudgate mcp` lit des requetes JSON-RPC 2.0 ligne par ligne sur `stdin` et ecrit sur `stdout`. N'ecrivez jamais de logs de debug sur `stdout`.
