"""Clean-room MCP stdio JSON-RPC 2.0 server exposing loudgate post-production tools."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import Any, Callable, TextIO

from .clean import CleanConfig, clean_file
from .cut import CutConfig, cut_silences
from .master import MasterConfig, master_file
from .probe import probe_file

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "loudgate"
SERVER_VERSION = "0.1.0"

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "probe_loudness",
        "description": (
            "Audit an audio file for EBU R128 integrated loudness (LUFS), LRA (LU), "
            "true peak (dBTP), 4x oversampled inter-sample peaks, DC offset, clipping, and SNR."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to input audio file."},
                "clip_threshold": {
                    "type": "number",
                    "description": "Amplitude threshold for sample clipping detection (default 0.999).",
                    "default": 0.999,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "clean_spectral",
        "description": (
            "Run deterministic DSP cleanup: DC removal, zero-phase Butterworth high-pass, "
            "de-clicker, STFT WOLA spectral denoising, optional de-esser and noise gate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "input_path": {"type": "string", "description": "Path to source audio file."},
                "output_path": {"type": "string", "description": "Path to cleaned output audio file."},
                "highpass_hz": {"type": "number", "default": 80.0},
                "denoise_strength": {"type": "number", "default": 1.0},
                "spectral_floor_db": {"type": "number", "default": -28.0},
                "declick": {"type": "boolean", "default": True},
                "deess_enabled": {"type": "boolean", "default": False},
                "gate_enabled": {"type": "boolean", "default": False},
                "n_fft": {"type": "integer", "default": 2048},
                "hop_length": {"type": "integer", "default": 512},
            },
            "required": ["input_path", "output_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "master_broadcast",
        "description": (
            "Normalize audio to broadcast/podcast loudness targets (-14, -16, -23 LUFS) "
            "with two-pass FFmpeg loudnorm and true-peak ceiling <= -1.0 dBTP."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "input_path": {"type": "string", "description": "Path to input audio file."},
                "output_path": {"type": "string", "description": "Path to mastered output audio file."},
                "preset": {
                    "type": "string",
                    "enum": ["web", "podcast", "mobile", "stereo", "broadcast", "ebu_r128"],
                    "default": "podcast",
                },
                "target_lufs": {"type": ["number", "null"], "default": None},
                "max_true_peak_dbtp": {"type": "number", "default": -1.0},
                "bitrate": {"type": "string", "default": "192k"},
            },
            "required": ["input_path", "output_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cut_silences_otio",
        "description": (
            "Detect silence vs active speech (RMS energy + ZCR) and export OpenTimelineIO (.otio), "
            "CMX 3600 EDL (.edl), SubRip (.srt), WebVTT (.vtt), and optional trimmed WAV."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "input_path": {"type": "string", "description": "Path to source audio file."},
                "output_dir": {"type": "string", "description": "Directory for timeline, subtitles, and cut WAV."},
                "silence_thresh_db": {"type": "number", "default": -40.0},
                "min_silence_ms": {"type": "number", "default": 400.0},
                "min_speech_ms": {"type": "number", "default": 150.0},
                "pad_ms": {"type": "number", "default": 80.0},
                "fps": {"type": "number", "default": 25.0},
                "render_audio": {"type": "boolean", "default": True},
            },
            "required": ["input_path", "output_dir"],
            "additionalProperties": False,
        },
    },
]


def _tool_probe_loudness(args: dict[str, Any]) -> dict[str, Any]:
    report = probe_file(
        path=args["path"],
        clip_threshold=float(args.get("clip_threshold", 0.999)),
    )
    return report.to_dict()


def _tool_clean_spectral(args: dict[str, Any]) -> dict[str, Any]:
    cfg = CleanConfig(
        highpass_hz=float(args.get("highpass_hz", 80.0)),
        denoise_strength=float(args.get("denoise_strength", 1.0)),
        spectral_floor_db=float(args.get("spectral_floor_db", -28.0)),
        declick=bool(args.get("declick", True)),
        deess_enabled=bool(args.get("deess_enabled", False)),
        gate_enabled=bool(args.get("gate_enabled", False)),
        n_fft=int(args.get("n_fft", 2048)),
        hop_length=int(args.get("hop_length", 512)),
    )
    stats = clean_file(args["input_path"], args["output_path"], config=cfg)
    payload = asdict(stats)
    payload["output_path"] = args["output_path"]
    return payload


def _tool_master_broadcast(args: dict[str, Any]) -> dict[str, Any]:
    raw_target = args.get("target_lufs")
    cfg = MasterConfig(
        preset=str(args.get("preset", "podcast")),
        target_lufs=float(raw_target) if raw_target is not None else None,
        max_true_peak_dbtp=float(args.get("max_true_peak_dbtp", -1.0)),
        bitrate=str(args.get("bitrate", "192k")),
    )
    res = master_file(args["input_path"], args["output_path"], config=cfg)
    return res.to_dict()


def _tool_cut_silences_otio(args: dict[str, Any]) -> dict[str, Any]:
    cfg = CutConfig(
        silence_thresh_db=float(args.get("silence_thresh_db", -40.0)),
        min_silence_ms=float(args.get("min_silence_ms", 400.0)),
        min_speech_ms=float(args.get("min_speech_ms", 150.0)),
        pad_ms=float(args.get("pad_ms", 80.0)),
        fps=float(args.get("fps", 25.0)),
    )
    return cut_silences(
        input_path=args["input_path"],
        output_dir=args["output_dir"],
        config=cfg,
        render_audio=bool(args.get("render_audio", True)),
    )


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "probe_loudness": _tool_probe_loudness,
    "clean_spectral": _tool_clean_spectral,
    "master_broadcast": _tool_master_broadcast,
    "cut_silences_otio": _tool_cut_silences_otio,
}


def dispatch_request(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Process one JSON-RPC 2.0 request/notification and return response dict or None."""
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "notifications/initialized" or (req_id is None and str(method).startswith("notifications/")):
        return None

    def _ok(result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _err(code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        client_proto = params.get("protocolVersion") or PROTOCOL_VERSION
        return _ok(
            {
                "protocolVersion": client_proto if isinstance(client_proto, str) else PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        )

    if method == "ping":
        return _ok({})

    if method == "tools/list":
        return _ok({"tools": TOOL_DEFINITIONS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or name not in TOOL_HANDLERS:
            return _err(-32602, f"Unknown tool: {name!r}")
        if not isinstance(args, dict):
            return _err(-32602, "Tool arguments must be a JSON object.")
        try:
            data = TOOL_HANDLERS[name](args)
            return _ok(
                {
                    "content": [{"type": "text", "text": json.dumps(data)}],
                    "isError": False,
                }
            )
        except Exception as exc:
            return _ok(
                {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                }
            )

    if req_id is None:
        return None
    return _err(-32601, f"Method not found: {method!r}")


def serve_stdio(stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Run line-delimited JSON-RPC 2.0 MCP server on stdio."""
    in_stream = stdin or sys.stdin
    out_stream = stdout or sys.stdout

    for raw_line in in_stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                raise ValueError("JSON-RPC message must be an object.")
        except Exception as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            out_stream.write(json.dumps(err_resp, separators=(",", ":")) + "\n")
            out_stream.flush()
            continue

        resp = dispatch_request(msg)
        if resp is not None:
            out_stream.write(json.dumps(resp, separators=(",", ":")) + "\n")
            out_stream.flush()
