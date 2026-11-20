# Reference CLI & API Python

[English Reference (EN)](../cli-reference.md) | [Index Documentation](index.md)

## Structure CLI globale

```bash
loudgate <commande> [options]
loudgate-ui [options]
```

Commandes disponibles :
- `loudgate probe` - Audit EBU R128 (LUFS, LRA, dBTP), crete 4x, offset DC, saturation et RSB
- `loudgate clean` - Passe-haut phase nulle, suppression DC, de-clicker MAD, debruitage STFT Wiener, de-esseur, gate
- `loudgate master` - Normalisation FFmpeg `loudnorm` deux passes + limiteur crete vraie 4x
- `loudgate cut` - Detection silences/VAD (RMS + ZCR) et export OTIO/EDL/SRT/VTT/WAV
- `loudgate ui` (ou `loudgate-ui`) - Interface graphique Studio locale en loopback
- `loudgate mcp` - Serveur MCP `stdio` JSON-RPC 2.0

---

## `loudgate probe`

```bash
loudgate probe <input> [--clip-threshold 0.999] [--json]
```

| Option | Defaut | Description |
| :--- | :--- | :--- |
| `input` | requis | Chemin du fichier audio source (WAV, FLAC, OGG, AIFF, MP3, M4A, AAC, OPUS). |
| `--clip-threshold` | `0.999` | Seuil d'amplitude absolue pour compter les echantillons satures. |
| `--json` | `false` | Sortie JSON `ProbeReport` complete. |

---

## `loudgate clean`

```bash
loudgate clean <input> <output> [options]
```

| Option | Defaut | Description |
| :--- | :--- | :--- |
| `--highpass-hz` | `80.0` | Frequence de coupure passe-haut Butterworth ordre 4 a phase nulle (`0` desactive). |
| `--denoise-strength` | `1.0` | Facteur de sur-soustraction Wiener (`0.0` contourne le debruitage). |
| `--spectral-floor-db` | `-28.0` | Gain spectral minimum en dB limitant le bruit musical. |
| `--n-fft` | `2048` | Taille FFT / fenetre STFT (multiple entier `>= 2` de `--hop-length`). |
| `--hop-length` | `512` | Pas entre trames STFT en echantillons. |
| `--no-declick` | `false` | Desactive la reparation des clics impulsionnels MAD. |
| `--deess` | `false` | Active l'attenuateur de sibilance bande haute (`5000` a `10000 Hz`). |
| `--gate` | `false` | Active le noise gate progressif (`-45 dBFS`, attenuation `-24 dB`). |
| `--json` | `false` | Sortie JSON `CleanStats` (`clicks_repaired`, `parseval_relative_error`, `noise_reduction_db_est`). |

---

## `loudgate master`

```bash
loudgate master <input> <output> [options]
```

| Option | Defaut | Description |
| :--- | :--- | :--- |
| `--preset` | `podcast` | Preset cible issu de `MASTER_PRESETS`. |
| `--target-lufs` | `None` | Remplace la cible de loudness integre du preset (`LUFS`). |
| `--max-tp` | `-1.0` | Plafond maximal de crete vraie (`dBTP`). |
| `--bitrate` | `192k` | Debit pour les exports compressifs (`.mp3`, `.aac`, `.m4a`, `.opus`). |
| `--dual-mono` | `false` | Active `dual_mono=true` dans FFmpeg `loudnorm`. |
| `--json` | `false` | Sortie JSON `MasterResult` avec `pass1_stats`, `pass2_stats` et `output_probe`. |

### Presets de Mastering (`MASTER_PRESETS`)

| Preset | Loudness Cible | Crete Vraie Max | LRA Cible | Usage typique |
| :--- | :--- | :--- | :--- | :--- |
| `podcast` | `-14.0 LUFS` | `-1.0 dBTP` | `11.0 LU` | Podcasts et voix parlee |
| `web` | `-14.0 LUFS` | `-1.0 dBTP` | `11.0 LU` | YouTube et diffusion web |
| `mobile` | `-16.0 LUFS` | `-1.0 dBTP` | `9.0 LU` | Ecoute mobile et applications vocales |
| `stereo` | `-16.0 LUFS` | `-1.0 dBTP` | `9.0 LU` | Apple Podcasts / stereo |
| `broadcast` | `-23.0 LUFS` | `-1.0 dBTP` | `7.0 LU` | Livraison broadcast europeenne |
| `ebu_r128` | `-23.0 LUFS` | `-1.0 dBTP` | `7.0 LU` | Conformite stricte EBU R128 |

---

## `loudgate cut`

```bash
loudgate cut <input> <output_dir> [options]
```

| Option | Defaut | Description |
| :--- | :--- | :--- |
| `--thresh-db` | `-40.0` | Seuil principal d'energie RMS (`dBFS`). |
| `--min-silence-ms` | `400.0` | Duree minimale de silence pour separer deux segments (ms). |
| `--min-speech-ms` | `150.0` | Duree minimale d'un ilot vocal conserve (ms). |
| `--pad-ms` | `80.0` | Marge ajoutee avant et apres chaque segment vocal (ms). |
| `--fps` | `25.0` | Cadence d'images utilisee dans OpenTimelineIO et EDL CMX 3600. |
| `--no-render` | `false` | Exporte uniquement `.otio`, `.edl`, `.srt`, `.vtt` sans rendre `_cut.wav`. |
| `--json` | `false` | Sortie JSON avec liste des segments et chemins generes. |

---

## `loudgate ui` / `loudgate-ui`

```bash
loudgate ui [--host 127.0.0.1] [--port 8742] [--workspace-dir .] [--no-open] [--desktop]
loudgate-ui [--host 127.0.0.1] [--port 8742] [--workspace-dir .] [--no-open] [--desktop]
```

---

## API Python

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
