"""MCP JSON-RPC 2.0 protocol and stdio subprocess tests for loudgate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from loudgate.audio_io import AudioBuffer, write_audio
from loudgate.mcp_server import dispatch_request


def test_mcp_dispatch_all_tools_end_to_end(tmp_path: Path) -> None:
    fs = 48000
    t = np.arange(fs * 2, dtype=np.float64) / fs
    rng = np.random.default_rng(11)
    y = 0.001 * rng.standard_normal(len(t))
    y[int(0.2 * fs) : int(1.0 * fs)] += 0.15 * np.sin(2.0 * np.pi * 500.0 * t[int(0.2 * fs) : int(1.0 * fs)])

    raw = tmp_path / "probe_sample.wav"
    cleaned = tmp_path / "cleaned_sample.wav"
    mastered = tmp_path / "mastered_sample.wav"
    cut_dir = tmp_path / "cut_dir"
    write_audio(raw, AudioBuffer(samples=y, sample_rate=fs), subtype="PCM_24")

    init_resp = dispatch_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
    )
    assert init_resp is not None
    assert init_resp["result"]["serverInfo"]["name"] == "loudgate"

    assert dispatch_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    list_resp = dispatch_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert list_resp is not None
    tool_names = {t["name"] for t in list_resp["result"]["tools"]}
    assert tool_names == {"probe_loudness", "clean_spectral", "master_broadcast", "cut_silences_otio"}

    probe_resp = dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "probe_loudness", "arguments": {"path": str(raw)}},
        }
    )
    assert probe_resp is not None
    assert not probe_resp["result"]["isError"]
    probe_payload = json.loads(probe_resp["result"]["content"][0]["text"])
    assert probe_payload["sample_rate"] == fs

    clean_resp = dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "clean_spectral",
                "arguments": {"input_path": str(raw), "output_path": str(cleaned)},
            },
        }
    )
    assert clean_resp is not None
    assert not clean_resp["result"]["isError"]
    assert cleaned.exists()

    master_resp = dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "master_broadcast",
                "arguments": {"input_path": str(cleaned), "output_path": str(mastered), "preset": "podcast"},
            },
        }
    )
    assert master_resp is not None
    assert not master_resp["result"]["isError"]
    master_payload = json.loads(master_resp["result"]["content"][0]["text"])
    assert master_payload["output_probe"]["true_peak_dbtp"] <= -1.0

    cut_resp = dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "cut_silences_otio",
                "arguments": {"input_path": str(mastered), "output_dir": str(cut_dir)},
            },
        }
    )
    assert cut_resp is not None
    assert not cut_resp["result"]["isError"]
    cut_payload = json.loads(cut_resp["result"]["content"][0]["text"])
    assert cut_payload["segment_count"] == 1


def test_mcp_stdio_subprocess_roundtrip(tmp_path: Path) -> None:
    fs = 48000
    t = np.arange(fs, dtype=np.float64) / fs
    wav = tmp_path / "qa_stdio_tone.wav"
    write_audio(wav, AudioBuffer(samples=0.2 * np.sin(2.0 * np.pi * 1000.0 * t), sample_rate=fs))

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "probe_loudness", "arguments": {"path": str(wav)}},
        },
    ]
    stdin_payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.run(
        [sys.executable, "-m", "loudgate", "mcp"],
        input=stdin_payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0
    lines = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
    assert len(lines) == 3
    assert lines[0]["id"] == 1
    assert lines[1]["id"] == 2
    assert len(lines[1]["result"]["tools"]) == 4
    probe_out = json.loads(lines[2]["result"]["content"][0]["text"])
    assert probe_out["sample_rate"] == fs
