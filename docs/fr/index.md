# Documentation loudgate

**Francais (FR)** | [English (EN)](../index.md)

`loudgate` est un moteur open-source de post-production audio broadcast en Python, NumPy/SciPy, soundfile et FFmpeg local. Tous les traitements s'executent de facon deterministe sur votre CPU sans envoi cloud, sans telemetrie ni abonnement.

## Guides disponibles

| Guide | Contenu |
| :--- | :--- |
| [Guide de l'interface Studio (`loudgate ui`)](gui-guide.md) | Lancer `loudgate ui` / `loudgate-ui`, charger un fichier ou l'echantillon integre, lire les vu-metres EBU R128, nettoyer par STFT, masteriser, decouper les silences et exporter les timelines. |
| [Reference CLI & API Python](cli-reference.md) | Reference complete des commandes `probe`, `clean`, `master`, `cut`, `ui`, `mcp`, des presets (`MASTER_PRESETS`), des options et de l'API Python. |
| [Configuration Serveur MCP](mcp-setup.md) | Connecter `loudgate mcp` (`stdio` JSON-RPC 2.0) a Claude Desktop, Cursor, VS Code ou des agents locaux. |

## Chaine de post-production

1. **Audit (`probe`)** : Mesure du loudness integre EBU R128 (`LUFS`), plage dynamique (`LRA`), crete vraie FFmpeg (`dBTP`), crete inter-echantillons surechantillonnee 4x, offset DC, echantillons satures et RSB estime (`dB`).
2. **Nettoyage (`clean`)** : Suppression DC, passe-haut Butterworth d'ordre 4 a phase nulle (`scipy.signal.sosfiltfilt`), reparation des clics impulsionnels par ecart median (MAD), filtre de Wiener STFT WOLA a fenetre racine de Hann periodique, de-esseur bande haute et noise gate progressif en option.
3. **Mastering (`master`)** : Normalisation lineaire deux passes `loudnorm` FFmpeg avec verification `ebur128` post-rendu et limiteur de securite crete vraie 4x.
4. **Decoupe des silences & Export NLE (`cut`)** : Segmentation parole / silence par energie RMS 20 ms et taux de passages par zero (ZCR), puis export OpenTimelineIO (`.otio`), CMX 3600 (`.edl`), SubRip (`.srt`), WebVTT (`.vtt`) et WAV decoupe (`.wav`).

## Prerequis systeme

- Python 3.10 ou plus recent
- `ffmpeg` et `ffprobe` installes et disponibles dans le `PATH`
- Dependances Python : `numpy>=1.24`, `scipy>=1.11`, `soundfile>=0.12`

## Installation rapide

```bash
git clone https://github.com/eldertek/loudgate.git
cd loudgate
pip install -e ".[dev]"
```

Lancer l'interface Studio locale :

```bash
loudgate ui
```
