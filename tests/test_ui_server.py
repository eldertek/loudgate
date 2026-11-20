"""Integration and security tests for loudgate local studio UI server."""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import pytest

from loudgate.audio_io import AudioBuffer, read_audio, write_audio
from loudgate.master import MASTER_PRESETS
from loudgate.ui.server import create_server, validate_loopback_host


@pytest.fixture()
def studio_url(tmp_path: Path):
    """Launch an ephemeral loopback studio server on port 0 scoped to tmp_path."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    server = create_server(host="127.0.0.1", port=0, workspace_dir=ws, max_upload_bytes=2 * 1024 * 1024)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base_url = f"http://{host}:{port}"
    try:
        yield base_url, ws
    finally:
        server.shutdown()
        server.close_studio()


def _http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, str], Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, dict(resp.headers), json.loads(body)
    except HTTPError as err:
        body = err.read().decode("utf-8")
        return err.code, dict(err.headers), json.loads(body)


def _http_raw(method: str, url: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    req = Request(url, headers=headers or {}, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except HTTPError as err:
        return err.code, dict(err.headers), err.read()


def _build_multipart_file(field_name: str, filename: str, content: bytes) -> tuple[str, bytes]:
    boundary = f"----loudgate-{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return f"multipart/form-data; boundary={boundary}", body


def test_cli_ui_help_and_loopback_enforcement() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "loudgate", "ui", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert proc.returncode == 0
    assert "--workspace-dir" in proc.stdout
    assert "--no-open" in proc.stdout

    proc_standalone = subprocess.run(
        [sys.executable, "-c", "from loudgate.ui import main; main(['--help'])"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert proc_standalone.returncode == 0
    assert "loudgate-ui" in proc_standalone.stdout

    assert validate_loopback_host("localhost") == "127.0.0.1"
    assert validate_loopback_host("127.0.0.1") == "127.0.0.1"
    with pytest.raises(ValueError, match="loopback"):
        validate_loopback_host("0.0.0.0")


def test_health_static_csp_and_frontend_hygiene(studio_url: tuple[str, Path]) -> None:
    base_url, _ = studio_url
    status, headers, health = _http_json("GET", f"{base_url}/api/health")
    assert status == 200
    assert health["status"] == "ok"
    assert health["presets"] == MASTER_PRESETS
    assert "default-src 'self'" in headers.get("Content-Security-Policy", "")
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("Cache-Control") == "no-store"

    for static_path in ("/", "/styles.css", "/app.js", "/i18n.js", "/waveform.js", "/favicon.svg"):
        st, hdrs, raw = _http_raw("GET", f"{base_url}{static_path}")
        assert st == 200, f"Failed static asset {static_path}"
        assert len(raw) > 0
        assert "script-src 'self'" in hdrs.get("Content-Security-Policy", "")
        text = raw.decode("utf-8", errors="replace")
        assert "\u2013" not in text
        assert "\u2014" not in text
        if static_path.endswith(".js"):
            assert "innerHTML" not in text
            assert "document.write" not in text
            assert "eval(" not in text


def test_path_traversal_and_upload_security_rejections(studio_url: tuple[str, Path], tmp_path: Path) -> None:
    base_url, _ = studio_url
    parsed = base_url.replace("http://", "")
    host, port_str = parsed.split(":")

    # Raw HTTPConnection preserves /../ sequences without urllib client normalization.
    for bad_path in ("/../../pyproject.toml", "/static/%2e%2e/%2e%2e/pyproject.toml", "/api/assets/../../audio"):
        conn = http.client.HTTPConnection(host, int(port_str), timeout=10)
        conn.request("GET", bad_path)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status in (400, 403)

    outside = tmp_path / "outside.wav"
    fs = 48000
    write_audio(outside, AudioBuffer(samples=np.zeros(fs // 4), sample_rate=fs))

    st_ws_rel, _, _ = _http_json("POST", f"{base_url}/api/workspace/load", {"path": "../outside.wav"})
    assert st_ws_rel in (400, 403)

    st_ws_abs, _, _ = _http_json("POST", f"{base_url}/api/workspace/load", {"path": str(outside)})
    assert st_ws_abs in (400, 403)

    # Bad asset id
    st_bad_id, _, _ = _http_raw("GET", f"{base_url}/api/assets/not_a_valid_id!/audio")
    assert st_bad_id == 400

    # Spoofed magic bytes uploaded as .wav must be rejected
    ctype, body = _build_multipart_file("file", "spoofed.wav", b"MZ\x90\x00NOT_A_WAV_HEADER_1234567890")
    req = Request(f"{base_url}/api/upload", data=body, headers={"Content-Type": ctype}, method="POST")
    with pytest.raises(HTTPError) as exc_info:
        urlopen(req, timeout=10)
    assert exc_info.value.code == 400

    # Oversized upload must be rejected with 413
    too_big = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * (2 * 1024 * 1024 + 128)
    ctype_big, body_big = _build_multipart_file("file", "huge.wav", too_big)
    req_big = Request(f"{base_url}/api/upload", data=body_big, headers={"Content-Type": ctype_big}, method="POST")
    with pytest.raises(HTTPError) as exc_big:
        urlopen(req_big, timeout=10)
    assert exc_big.value.code == 413


def test_full_studio_dsp_pipeline_sample_clean_master_cut(studio_url: tuple[str, Path], tmp_path: Path) -> None:
    base_url, ws = studio_url

    # 1. Load built-in broadcast sample
    st, _, sample = _http_json("POST", f"{base_url}/api/sample", {})
    assert st == 200
    src_id = sample["asset_id"]
    assert sample["probe"]["sample_rate"] == 48000
    assert sample["probe"]["max_abs_dc_offset"] > 0.01
    assert sample["waveform"]["bins"] > 100

    # Verify HTTP Range byte streaming works for browser <audio> seeking
    st_rng, rng_hdrs, rng_bytes = _http_raw(
        "GET",
        f"{base_url}{sample['audio_url']}",
        headers={"Range": "bytes=0-1023"},
    )
    assert st_rng == 206
    assert len(rng_bytes) == 1024
    assert rng_hdrs.get("Accept-Ranges") == "bytes"
    assert rng_hdrs.get("Content-Range", "").startswith("bytes 0-1023/")

    # 2. Validation error on invalid clean hop_length
    st_bad_clean, _, bad_clean = _http_json(
        "POST",
        f"{base_url}/api/clean",
        {"asset_id": src_id, "n_fft": 2048, "hop_length": 500},
    )
    assert st_bad_clean == 400
    assert bad_clean["code"] == "validation_error"

    # 3. Run real STFT clean
    st_clean, _, clean_res = _http_json(
        "POST",
        f"{base_url}/api/clean",
        {
            "asset_id": src_id,
            "highpass_hz": 80.0,
            "denoise_strength": 1.0,
            "spectral_floor_db": -28.0,
            "n_fft": 2048,
            "hop_length": 512,
            "declick": True,
            "deess_enabled": True,
            "gate_enabled": False,
        },
    )
    assert st_clean == 200
    assert clean_res["stats"]["clicks_repaired"] >= 3
    assert clean_res["stats"]["parseval_relative_error"] < 1e-12
    assert clean_res["after_probe"]["max_abs_dc_offset"] < 1e-4
    assert clean_res["after_probe"]["estimated_snr_db"] > clean_res["before_probe"]["estimated_snr_db"]

    cleaned_id = clean_res["output_asset"]["asset_id"]
    st_dl_c, _, dl_c_bytes = _http_raw("GET", f"{base_url}{clean_res['output_asset']['download_url']}")
    assert st_dl_c == 200
    assert dl_c_bytes[:4] == b"RIFF"

    # 4. Run real Two-Pass Broadcast Mastering on cleaned audio
    st_master, _, master_res = _http_json(
        "POST",
        f"{base_url}/api/master",
        {
            "asset_id": cleaned_id,
            "preset": "podcast",
            "max_true_peak_dbtp": -1.0,
            "format": "wav",
        },
    )
    assert st_master == 200
    assert abs(master_res["after_probe"]["integrated_lufs"] - (-14.0)) <= 1.2
    assert master_res["after_probe"]["true_peak_dbtp"] <= -1.0
    mastered_id = master_res["output_asset"]["asset_id"]

    # 5. Run real VAD Silence Cut & Timeline Export
    st_cut, _, cut_res = _http_json(
        "POST",
        f"{base_url}/api/cut",
        {
            "asset_id": mastered_id,
            "silence_thresh_db": -40.0,
            "min_silence_ms": 400.0,
            "min_speech_ms": 150.0,
            "pad_ms": 80.0,
            "fps": 25.0,
            "render_audio": True,
        },
    )
    assert st_cut == 200
    assert cut_res["segment_count"] == 4
    assert cut_res["removed_duration_s"] > 1.5
    assert cut_res["trimmed_asset"] is not None

    # Verify all 5 downloads (.otio, .edl, .srt, .vtt, .wav)
    for kind in ("otio", "edl", "srt", "vtt", "wav"):
        st_exp, _, exp_bytes = _http_raw("GET", f"{base_url}{cut_res['downloads'][kind]}")
        assert st_exp == 200
        assert len(exp_bytes) > 0
        if kind == "otio":
            otio = json.loads(exp_bytes.decode("utf-8"))
            assert otio["OTIO_SCHEMA"] == "Timeline.1"
            assert len(otio["tracks"]["children"][0]["children"]) == 4
        elif kind == "edl":
            assert "FCM: NON-DROP FRAME" in exp_bytes.decode("utf-8")
        elif kind == "srt":
            assert "-->" in exp_bytes.decode("utf-8")
        elif kind == "vtt":
            assert exp_bytes.decode("utf-8").startswith("WEBVTT")
        elif kind == "wav":
            cut_wav = tmp_path / "downloaded_cut.wav"
            cut_wav.write_bytes(exp_bytes)
            cut_buf = read_audio(cut_wav)
            assert abs(cut_buf.duration_s - cut_res["kept_duration_s"]) < 0.08

    # 6. Verify session persistence snapshot & workspace/upload loading
    st_sess, _, sess = _http_json("GET", f"{base_url}/api/session")
    assert st_sess == 200
    assert sess["active_source"]["asset_id"] == src_id
    assert sess["clean"] is not None
    assert sess["master"] is not None
    assert sess["cut"] is not None

    # Create workspace file and load it
    ws_wav = ws / "subdir" / "voice_ws.wav"
    fs = 48000
    t = np.arange(fs, dtype=np.float64) / fs
    write_audio(ws_wav, AudioBuffer(samples=0.2 * np.sin(2.0 * np.pi * 440.0 * t), sample_rate=fs))

    st_list, _, ws_list = _http_json("GET", f"{base_url}/api/workspace/files")
    assert st_list == 200
    assert any(f["path"] == "subdir/voice_ws.wav" for f in ws_list["files"])

    st_load, _, loaded = _http_json("POST", f"{base_url}/api/workspace/load", {"path": "subdir/voice_ws.wav"})
    assert st_load == 200
    assert loaded["source_kind"] == "workspace"

    # Multipart upload of real WAV
    ctype_ok, body_ok = _build_multipart_file("file", "uploaded_tone.wav", ws_wav.read_bytes())
    req_ok = Request(f"{base_url}/api/upload", data=body_ok, headers={"Content-Type": ctype_ok}, method="POST")
    with urlopen(req_ok, timeout=15) as resp_ok:
        assert resp_ok.status == 200
        up_json = json.loads(resp_ok.read().decode("utf-8"))
        assert up_json["source_kind"] == "upload"
        assert up_json["probe"]["sample_rate"] == 48000
