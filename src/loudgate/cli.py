"""Command-line interface for loudgate."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Sequence

from .clean import CleanConfig, clean_file
from .cut import CutConfig, cut_silences
from .master import MASTER_PRESETS, MasterConfig, master_file
from .mcp_server import serve_stdio
from .probe import probe_file
from .ui.server import DEFAULT_HOST, DEFAULT_PORT, serve_ui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loudgate",
        description="Clean-room broadcast audio post-production engine.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # probe
    p_probe = sub.add_parser("probe", help="Audit EBU R128 loudness, peaks, DC offset, clipping, and SNR.")
    p_probe.add_argument("input", help="Input audio file path.")
    p_probe.add_argument("--clip-threshold", type=float, default=0.999)
    p_probe.add_argument("--json", action="store_true", help="Emit JSON output.")

    # clean
    p_clean = sub.add_parser("clean", help="Run STFT spectral denoising, zero-phase HPF, and de-clicker.")
    p_clean.add_argument("input", help="Input audio file path.")
    p_clean.add_argument("output", help="Output cleaned audio file path.")
    p_clean.add_argument("--highpass-hz", type=float, default=80.0)
    p_clean.add_argument("--denoise-strength", type=float, default=1.0)
    p_clean.add_argument("--spectral-floor-db", type=float, default=-28.0)
    p_clean.add_argument("--n-fft", type=int, default=2048)
    p_clean.add_argument("--hop-length", type=int, default=512)
    p_clean.add_argument("--no-declick", action="store_true")
    p_clean.add_argument("--deess", action="store_true")
    p_clean.add_argument("--gate", action="store_true")
    p_clean.add_argument("--json", action="store_true", help="Emit JSON output.")

    # master
    p_master = sub.add_parser("master", help="Normalize to EBU R128 / podcast / mobile loudness targets.")
    p_master.add_argument("input", help="Input audio file path.")
    p_master.add_argument("output", help="Output mastered audio file path.")
    p_master.add_argument("--preset", choices=sorted(MASTER_PRESETS), default="podcast")
    p_master.add_argument("--target-lufs", type=float, default=None)
    p_master.add_argument("--max-tp", type=float, default=-1.0)
    p_master.add_argument("--bitrate", default="192k")
    p_master.add_argument("--dual-mono", action="store_true")
    p_master.add_argument("--json", action="store_true", help="Emit JSON output.")

    # cut
    p_cut = sub.add_parser("cut", help="Detect silences and export OTIO, EDL, SRT/VTT, and trimmed WAV.")
    p_cut.add_argument("input", help="Input audio file path.")
    p_cut.add_argument("output_dir", help="Output directory for timeline, subtitles, and cut audio.")
    p_cut.add_argument("--thresh-db", type=float, default=-40.0)
    p_cut.add_argument("--min-silence-ms", type=float, default=400.0)
    p_cut.add_argument("--min-speech-ms", type=float, default=150.0)
    p_cut.add_argument("--pad-ms", type=float, default=80.0)
    p_cut.add_argument("--fps", type=float, default=25.0)
    p_cut.add_argument("--no-render", action="store_true", help="Export timelines and subtitles only.")
    p_cut.add_argument("--json", action="store_true", help="Emit JSON output.")

    # mcp
    sub.add_parser("mcp", help="Run loudgate MCP stdio server.")

    # ui
    p_ui = sub.add_parser("ui", help="Launch local broadcast audio studio GUI (127.0.0.1 loopback).")
    p_ui.add_argument("--host", default=DEFAULT_HOST, help="Loopback bind host (default: 127.0.0.1).")
    p_ui.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port (default: 8742; 0 for auto).")
    p_ui.add_argument("--workspace-dir", default=".", help="Workspace directory for local audio file selection.")
    p_ui.add_argument("--no-open", action="store_true", help="Do not open browser automatically.")
    p_ui.add_argument(
        "--desktop",
        action="store_true",
        help="Launch in standalone desktop app window mode (pywebview or Edge/Chrome --app).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "mcp":
        serve_stdio()
        return 0

    if args.command == "ui":
        try:
            serve_ui(
                host=args.host,
                port=args.port,
                workspace_dir=args.workspace_dir,
                open_browser=not args.no_open,
                desktop=args.desktop,
            )
        except ValueError as exc:
            parser.error(str(exc))
            return 2
        return 0

    if args.command == "probe":
        report = probe_file(args.input, clip_threshold=args.clip_threshold)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(
                f"{report.path} | {report.sample_rate} Hz {report.channels}ch {report.duration_s:.2f}s | "
                f"I={report.integrated_lufs:.2f} LUFS LRA={report.lra_lu:.2f} LU "
                f"TP={report.true_peak_dbtp:.2f} dBTP OS_TP={report.oversampled_true_peak_dbtp:.2f} dBTP "
                f"SNR={report.estimated_snr_db:.1f} dB DC={report.max_abs_dc_offset:.6f} "
                f"Clipped={report.clipped_samples}"
            )
        return 0

    if args.command == "clean":
        cfg = CleanConfig(
            highpass_hz=args.highpass_hz,
            denoise_strength=args.denoise_strength,
            spectral_floor_db=args.spectral_floor_db,
            n_fft=args.n_fft,
            hop_length=args.hop_length,
            declick=not args.no_declick,
            deess_enabled=args.deess,
            gate_enabled=args.gate,
        )
        stats = clean_file(args.input, args.output, config=cfg)
        if args.json:
            print(json.dumps(asdict(stats), indent=2))
        else:
            print(
                f"Cleaned -> {args.output} | fs={stats.sample_rate} Hz "
                f"n_fft={stats.n_fft} hop={stats.hop_length} clicks={stats.clicks_repaired} "
                f"parseval_err={stats.parseval_relative_error:.2e} nr_est={stats.noise_reduction_db_est:.2f} dB"
            )
        return 0

    if args.command == "master":
        cfg = MasterConfig(
            preset=args.preset,
            target_lufs=args.target_lufs,
            max_true_peak_dbtp=args.max_tp,
            bitrate=args.bitrate,
            dual_mono=args.dual_mono,
        )
        res = master_file(args.input, args.output, config=cfg)
        if args.json:
            print(json.dumps(res.to_dict(), indent=2))
        else:
            p = res.output_probe
            print(
                f"Mastered ({res.preset}, {res.loudnorm_mode}) -> {res.output_path} | "
                f"I={p.integrated_lufs:.2f} LUFS TP={p.true_peak_dbtp:.2f} dBTP LRA={p.lra_lu:.2f} LU"
            )
        return 0

    if args.command == "cut":
        cfg = CutConfig(
            silence_thresh_db=args.thresh_db,
            min_silence_ms=args.min_silence_ms,
            min_speech_ms=args.min_speech_ms,
            pad_ms=args.pad_ms,
            fps=args.fps,
        )
        res = cut_silences(
            args.input,
            args.output_dir,
            config=cfg,
            render_audio=not args.no_render,
        )
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(
                f"Cut {res['segment_count']} segments | kept {res['kept_duration_s']:.2f}s / "
                f"{res['input_duration_s']:.2f}s | OTIO={res['otio_path']}"
            )
        return 0

    parser.print_help(sys.stderr)
    return 2
