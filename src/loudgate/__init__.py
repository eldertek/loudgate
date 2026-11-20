"""loudgate: Clean-room broadcast audio post-production engine."""

from .audio_io import AudioBuffer, ffprobe_metadata, read_audio, write_audio
from .clean import (
    CleanConfig,
    CleanStats,
    build_tight_stft,
    clean_audio,
    clean_file,
    declick_signal,
    spectral_denoise,
    verify_parseval_energy,
    zero_phase_highpass,
)
from .cut import (
    CutConfig,
    SpeechSegment,
    build_ffmpeg_trim_filtergraph,
    cut_silences,
    detect_speech_segments,
    export_edl,
    export_otio,
    export_srt,
    export_vtt,
    render_cut_audio,
)
from .master import MASTER_PRESETS, MasterConfig, MasterResult, master_file
from .probe import ProbeReport, probe_buffer, probe_file

__version__ = "0.1.0"

__all__ = [
    "AudioBuffer",
    "CleanConfig",
    "CleanStats",
    "CutConfig",
    "MASTER_PRESETS",
    "MasterConfig",
    "MasterResult",
    "ProbeReport",
    "SpeechSegment",
    "__version__",
    "build_ffmpeg_trim_filtergraph",
    "build_tight_stft",
    "clean_audio",
    "clean_file",
    "cut_silences",
    "declick_signal",
    "detect_speech_segments",
    "export_edl",
    "export_otio",
    "export_srt",
    "export_vtt",
    "ffprobe_metadata",
    "master_file",
    "probe_buffer",
    "probe_file",
    "read_audio",
    "render_cut_audio",
    "spectral_denoise",
    "verify_parseval_energy",
    "write_audio",
    "zero_phase_highpass",
]
