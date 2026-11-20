"""Local loopback HTTP studio server for loudgate UI."""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from dataclasses import asdict, dataclass
from email.parser import BytesParser
from email.policy import default as email_default_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlparse

import numpy as np
from scipy import signal

from .. import __version__
from ..audio_io import AudioBuffer, read_audio, write_audio
from ..clean import CleanConfig, clean_file
from ..cut import CutConfig, cut_silences
from ..master import MASTER_PRESETS, MasterConfig, master_file
from ..probe import ProbeReport, probe_file

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8742
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MiB default ceiling
MAX_JSON_BODY_BYTES = 256 * 1024
ALLOWED_AUDIO_EXTENSIONS = {".wav", ".flac", ".ogg", ".aiff", ".aif", ".mp3", ".m4a", ".aac", ".opus"}
ALLOWED_MASTER_FORMATS = {"wav", "flac", "mp3", "m4a", "aac", "opus"}
SKIP_SCAN_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache"}
SAFE_ID_RE = re.compile(r"^[a-f0-9]{16,32}$")
BITRATE_RE = re.compile(r"^\d{2,4}k$")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

STATIC_DIR = (Path(__file__).resolve().parent / "static").resolve()

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".opus": "audio/opus",
    ".otio": "application/json; charset=utf-8",
    ".edl": "text/plain; charset=utf-8",
    ".srt": "text/plain; charset=utf-8",
    ".vtt": "text/vtt; charset=utf-8",
}

CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "media-src 'self' blob:; "
    "img-src 'self' data: blob:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


class StudioError(Exception):
    """Structured HTTP error with status code and error code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class AssetRecord:
    """Registered audio file inside the studio session."""

    asset_id: str
    label: str
    filename: str
    path: Path
    source_kind: str
    probe: ProbeReport
    waveform: dict[str, Any]

    def to_summary(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "label": self.label,
            "filename": self.filename,
            "source_kind": self.source_kind,
            "audio_url": f"/api/assets/{self.asset_id}/audio",
            "download_url": f"/api/assets/{self.asset_id}/download",
            "probe": self.probe.to_dict(),
            "waveform": self.waveform,
        }


@dataclass
class ExportBundle:
    """Exported timeline, subtitle, and trimmed audio files from silence cutting."""

    export_id: str
    stem: str
    files: dict[str, Path]


def validate_loopback_host(host: str) -> str:
    """Ensure the server binds strictly to a loopback interface."""
    normalized = host.strip()
    if not normalized:
        raise ValueError("Host must not be empty.")
    if normalized.lower() == "localhost":
        return "127.0.0.1"
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise ValueError(f"Host '{host}' is not a valid loopback IP address.") from exc
    if not ip.is_loopback:
        raise ValueError(f"Refusing non-loopback host '{host}'. loudgate ui binds strictly to loopback.")
    return str(ip)


def _matches_audio_magic(header: bytes, ext: str) -> bool:
    """Verify leading file signature bytes match the declared audio extension."""
    if len(header) < 12:
        return False
    ext = ext.lower()
    if ext == ".wav":
        return header[:4] in (b"RIFF", b"RF64") and header[8:12] == b"WAVE"
    if ext == ".flac":
        return header[:4] == b"fLaC"
    if ext in {".ogg", ".opus"}:
        return header[:4] == b"OggS"
    if ext in {".aiff", ".aif"}:
        return header[:4] == b"FORM" and header[8:12] in (b"AIFF", b"AIFC")
    if ext == ".mp3":
        return header[:3] == b"ID3" or (header[0] == 0xFF and (header[1] & 0xE0) == 0xE0)
    if ext == ".aac":
        return header[:4] == b"ADIF" or (header[0] == 0xFF and (header[1] & 0xF0) == 0xF0)
    if ext == ".m4a":
        return header[4:8] == b"ftyp"
    return False


def _sanitize_filename(name: str, fallback: str = "audio.wav") -> str:
    base = Path(name).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return cleaned or fallback


def compute_spectrogram(
    mono: np.ndarray,
    fs: int,
    cols: int = 240,
    rows: int = 64,
) -> dict[str, Any]:
    """Compute log-frequency STFT power spectrogram quantized to base64 uint8."""
    f_min = 40.0
    f_max = min(20000.0, 0.49 * fs)
    if len(mono) < 64 or f_max <= f_min:
        return {"cols": 0, "rows": 0, "f_min_hz": f_min, "f_max_hz": f_max, "data_b64": ""}

    x = mono - np.mean(mono)
    n_fft = 1024 if len(x) >= 1024 else 256
    hop = max(64, n_fft // 4)
    win = signal.windows.hann(n_fft, sym=False)
    stft_obj = signal.ShortTimeFFT(win=win, hop=hop, fs=fs, mfft=n_fft)
    spec = np.abs(stft_obj.stft(x)) ** 2  # shape: (n_freqs, n_frames)
    freqs = stft_obj.f

    # Map linear FFT bins onto logarithmic frequency rows (high freq at row 0 -> low at bottom).
    band_edges = np.geomspace(f_min, f_max, rows + 1)
    band_spec = np.zeros((rows, spec.shape[1]), dtype=np.float64)
    for r in range(rows):
        mask = (freqs >= band_edges[r]) & (freqs <= band_edges[r + 1])
        if not np.any(mask):
            nearest = int(np.argmin(np.abs(freqs - 0.5 * (band_edges[r] + band_edges[r + 1]))))
            band_spec[rows - 1 - r, :] = spec[nearest, :]
        else:
            band_spec[rows - 1 - r, :] = np.max(spec[mask, :], axis=0)

    # Downsample time frames to cols using max-pooling so transients/clicks remain visible.
    n_frames = band_spec.shape[1]
    out_cols = min(cols, max(1, n_frames))
    frame_edges = np.linspace(0, n_frames, out_cols + 1, dtype=np.int64)
    pooled = np.zeros((rows, out_cols), dtype=np.float64)
    for c in range(out_cols):
        sl = band_spec[:, frame_edges[c] : max(frame_edges[c] + 1, frame_edges[c + 1])]
        pooled[:, c] = np.max(sl, axis=1)

    db = 10.0 * np.log10(np.maximum(pooled, 1e-12))
    norm = np.clip((db - (-85.0)) / 80.0, 0.0, 1.0)
    quantized = np.round(norm * 255.0).astype(np.uint8)
    b64 = base64.b64encode(quantized.tobytes()).decode("ascii")
    return {
        "cols": int(out_cols),
        "rows": int(rows),
        "f_min_hz": float(f_min),
        "f_max_hz": float(f_max),
        "data_b64": b64,
    }


def compute_waveform_peaks(audio: AudioBuffer, target_bins: int = 800) -> dict[str, Any]:
    """Downsample mono waveform and STFT spectrogram into compact Canvas payloads."""
    mono = audio.to_mono()
    n = len(mono)
    if n == 0:
        return {
            "duration_s": 0.0,
            "sample_rate": audio.sample_rate,
            "channels": audio.channels,
            "bins": 0,
            "mins": [],
            "maxs": [],
            "rms": [],
            "spectrogram": {"cols": 0, "rows": 0, "f_min_hz": 40.0, "f_max_hz": 20000.0, "data_b64": ""},
        }

    bins = min(max(1, target_bins), n)
    edges = np.linspace(0, n, bins + 1, dtype=np.int64)
    mins: list[float] = []
    maxs: list[float] = []
    rms: list[float] = []

    for i in range(bins):
        seg = mono[edges[i] : edges[i + 1]]
        if seg.size == 0:
            mins.append(0.0)
            maxs.append(0.0)
            rms.append(0.0)
            continue
        mins.append(round(float(np.min(seg)), 5))
        maxs.append(round(float(np.max(seg)), 5))
        rms.append(round(float(np.sqrt(np.mean(seg * seg))), 5))

    return {
        "duration_s": round(audio.duration_s, 6),
        "sample_rate": audio.sample_rate,
        "channels": audio.channels,
        "bins": bins,
        "mins": mins,
        "maxs": maxs,
        "rms": rms,
        "spectrogram": compute_spectrogram(mono, audio.sample_rate),
    }


def generate_broadcast_sample(out_path: str | Path, fs: int = 48000) -> Path:
    """Synthesize a deterministic broadcast speech+silence WAV with rumble, noise, DC, and clicks."""
    rng = np.random.default_rng(202601)
    duration_s = 6.4
    n = int(round(duration_s * fs))
    t = np.arange(n, dtype=np.float64) / fs

    # Background defects: room hiss (~ -54 dBFS), 38 Hz rumble (~ -50 dBFS), and small DC offset.
    y = 0.0020 * rng.standard_normal(n) + 0.0032 * np.sin(2.0 * np.pi * 38.0 * t) + 0.015

    # Four distinct speech-like formant phrases separated by clear >= 500 ms pauses.
    phrases = [
        (0.45, 1.55, 120.0, 148.0, False),
        (2.15, 3.25, 132.0, 118.0, True),
        (3.85, 4.85, 116.0, 142.0, True),
        (5.35, 6.05, 128.0, 110.0, False),
    ]
    b_sib, a_sib = signal.butter(4, [5500.0, 8500.0], btype="bandpass", fs=fs)

    for idx, (s0, s1, f_start, f_end, with_sibilance) in enumerate(phrases):
        i0 = int(round(s0 * fs))
        i1 = int(round(s1 * fs))
        seg_n = i1 - i0
        tau = np.arange(seg_n, dtype=np.float64) / fs
        seg_dur = seg_n / fs

        # Syllabic modulation + smooth tapered ends.
        syllables = 0.55 + 0.45 * np.sin(2.0 * np.pi * (3.2 + 0.3 * idx) * tau)
        edge = min(int(0.03 * fs), seg_n // 4)
        win = np.ones(seg_n, dtype=np.float64)
        if edge > 0:
            win[:edge] = np.linspace(0.0, 1.0, edge)
            win[-edge:] = np.linspace(1.0, 0.0, edge)
        env = win * syllables

        f0 = np.linspace(f_start, f_end, seg_n)
        phase = 2.0 * np.pi * np.cumsum(f0) / fs
        voiced = (
            0.55 * np.sin(phase)
            + 0.35 * np.sin(2.0 * phase + 0.3)
            + 0.25 * np.sin(4.0 * phase + 0.7)
            + 0.14 * np.sin(7.0 * phase)
        )
        phrase = 0.36 * env * voiced

        if with_sibilance and seg_dur > 0.4:
            sib_noise = signal.lfilter(b_sib, a_sib, rng.standard_normal(seg_n))
            sib_mask = np.exp(-0.5 * ((tau - (seg_dur - 0.14)) / 0.035) ** 2)
            phrase += 0.22 * sib_mask * sib_noise

        y[i0:i1] += phrase

    # Isolated single-sample digital mic clicks for declicker demonstration.
    for click_t in (0.12, 2.60, 4.40):
        c_idx = int(round(click_t * fs))
        if 1 <= c_idx < n - 1:
            y[c_idx] += 0.58

    y = np.clip(y, -0.98, 0.98)
    dst = Path(out_path)
    return write_audio(dst, AudioBuffer(samples=y, sample_rate=fs), subtype="PCM_24")


class StudioSession:
    """Thread-safe workspace confinement, asset registry, and DSP orchestration."""

    def __init__(
        self,
        workspace_dir: str | Path,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
    ) -> None:
        self.workspace_dir = Path(os.path.realpath(str(workspace_dir)))
        if not self.workspace_dir.exists() or not self.workspace_dir.is_dir():
            raise ValueError(f"Workspace directory does not exist: {self.workspace_dir}")
        self.max_upload_bytes = max_upload_bytes
        self._tmpdir_obj = tempfile.TemporaryDirectory(prefix="loudgate_studio_")
        self.session_dir = Path(os.path.realpath(self._tmpdir_obj.name))
        self.assets_dir = self.session_dir / "assets"
        self.exports_dir = self.session_dir / "exports"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._assets: dict[str, AssetRecord] = {}
        self._exports: dict[str, ExportBundle] = {}
        self._state: dict[str, Any] = {
            "active_source_id": None,
            "clean": None,
            "master": None,
            "cut": None,
        }

    def close(self) -> None:
        """Remove ephemeral session storage cleanly."""
        self._tmpdir_obj.cleanup()

    def resolve_workspace_file(self, rel_path: str) -> Path:
        """Confine a relative path strictly within workspace_dir via realpath."""
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise StudioError(HTTPStatus.BAD_REQUEST, "invalid_path", "File path must be a non-empty string.")
        if "\x00" in rel_path:
            raise StudioError(HTTPStatus.BAD_REQUEST, "invalid_path", "Null byte in path.")

        raw = rel_path.strip()
        if raw.startswith(("/", "\\")) or (len(raw) >= 2 and raw[1] == ":"):
            raise StudioError(HTTPStatus.FORBIDDEN, "path_outside_workspace", "Absolute paths are forbidden.")

        parts = re.split(r"[\\/]+", raw)
        if any(part == ".." for part in parts):
            raise StudioError(HTTPStatus.FORBIDDEN, "path_traversal", "Path traversal is forbidden.")

        ws_real = os.path.realpath(str(self.workspace_dir))
        candidate_real = os.path.realpath(os.path.join(ws_real, *parts))
        try:
            common = os.path.commonpath([ws_real, candidate_real])
        except ValueError as exc:
            raise StudioError(HTTPStatus.FORBIDDEN, "path_outside_workspace", "Path escapes workspace.") from exc

        if common != ws_real or candidate_real == ws_real:
            raise StudioError(HTTPStatus.FORBIDDEN, "path_outside_workspace", "Path escapes workspace directory.")

        candidate = Path(candidate_real)
        if not candidate.exists() or not candidate.is_file():
            raise StudioError(HTTPStatus.NOT_FOUND, "file_not_found", f"Audio file not found: {rel_path}")
        if candidate.suffix.lower() not in ALLOWED_AUDIO_EXTENSIONS:
            raise StudioError(
                HTTPStatus.BAD_REQUEST,
                "unsupported_extension",
                f"Unsupported audio file extension: {candidate.suffix}",
            )
        if candidate.stat().st_size > self.max_upload_bytes:
            raise StudioError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "file_too_large",
                f"File exceeds {self.max_upload_bytes} byte workspace limit.",
            )
        return candidate

    def list_workspace_files(self, max_files: int = 80, max_depth: int = 3) -> list[dict[str, Any]]:
        """List readable audio files inside workspace_dir safely."""
        items: list[dict[str, Any]] = []
        ws_real = str(self.workspace_dir)

        for root, dirs, files in os.walk(ws_real, followlinks=False):
            rel_root = os.path.relpath(root, ws_real)
            depth = 0 if rel_root == "." else len(Path(rel_root).parts)
            dirs[:] = sorted(d for d in dirs if d not in SKIP_SCAN_DIRS and not d.startswith("."))
            if depth >= max_depth:
                dirs[:] = []

            for fname in sorted(files):
                if len(items) >= max_files:
                    return items
                fpath = Path(root) / fname
                if fpath.suffix.lower() not in ALLOWED_AUDIO_EXTENSIONS:
                    continue
                try:
                    real = os.path.realpath(str(fpath))
                    if os.path.commonpath([ws_real, real]) != ws_real:
                        continue
                    stat = fpath.stat()
                    if stat.st_size <= self.max_upload_bytes:
                        items.append(
                            {
                                "path": Path(os.path.relpath(real, ws_real)).as_posix(),
                                "size_bytes": stat.st_size,
                            }
                        )
                except OSError:
                    continue
        return items

    def _register_asset(
        self,
        path: Path,
        label: str,
        filename: str,
        source_kind: str,
        set_active: bool = False,
        clip_threshold: float = 0.999,
    ) -> AssetRecord:
        probe = probe_file(path, clip_threshold=clip_threshold)
        audio = read_audio(path)
        waveform = compute_waveform_peaks(audio)
        asset_id = uuid.uuid4().hex[:20]
        rec = AssetRecord(
            asset_id=asset_id,
            label=label,
            filename=filename,
            path=path,
            source_kind=source_kind,
            probe=probe,
            waveform=waveform,
        )
        with self._lock:
            self._assets[asset_id] = rec
            if set_active:
                self._state["active_source_id"] = asset_id
                self._state["clean"] = None
                self._state["master"] = None
                self._state["cut"] = None
        return rec

    def get_asset(self, asset_id: str) -> AssetRecord:
        if not isinstance(asset_id, str) or not SAFE_ID_RE.match(asset_id):
            raise StudioError(HTTPStatus.BAD_REQUEST, "invalid_asset_id", "Malformed asset identifier.")
        with self._lock:
            rec = self._assets.get(asset_id)
        if rec is None or not rec.path.exists():
            raise StudioError(HTTPStatus.NOT_FOUND, "asset_not_found", "Audio asset not found in active session.")
        return rec

    def get_export_file(self, export_id: str, kind: str) -> tuple[Path, str]:
        if not isinstance(export_id, str) or not SAFE_ID_RE.match(export_id):
            raise StudioError(HTTPStatus.BAD_REQUEST, "invalid_export_id", "Malformed export identifier.")
        with self._lock:
            bundle = self._exports.get(export_id)
        if bundle is None or kind not in bundle.files:
            raise StudioError(HTTPStatus.NOT_FOUND, "export_not_found", "Requested export artifact was not found.")
        fpath = bundle.files[kind]
        if not fpath.exists():
            raise StudioError(HTTPStatus.NOT_FOUND, "export_not_found", "Export file is no longer available.")
        return fpath, fpath.name

    def get_session_snapshot(self) -> dict[str, Any]:
        with self._lock:
            active_id = self._state["active_source_id"]
            active_rec = self._assets.get(active_id) if active_id else None
            if active_rec is not None and not active_rec.path.exists():
                active_rec = None
                self._state["active_source_id"] = None
            return {
                "active_source": active_rec.to_summary() if active_rec else None,
                "clean": self._state["clean"],
                "master": self._state["master"],
                "cut": self._state["cut"],
            }

    def reset_session(self) -> dict[str, Any]:
        with self._lock:
            self._state = {
                "active_source_id": None,
                "clean": None,
                "master": None,
                "cut": None,
            }
        return self.get_session_snapshot()

    def set_active_source(self, asset_id: str) -> dict[str, Any]:
        rec = self.get_asset(asset_id)
        with self._lock:
            self._state["active_source_id"] = rec.asset_id
            self._state["clean"] = None
            self._state["master"] = None
            self._state["cut"] = None
        return self.get_session_snapshot()

    def load_built_in_sample(self) -> dict[str, Any]:
        sample_id = uuid.uuid4().hex[:12]
        sample_path = self.assets_dir / f"sample_{sample_id}.wav"
        generate_broadcast_sample(sample_path)
        rec = self._register_asset(
            path=sample_path,
            label="Broadcast Speech + Silence Sample",
            filename="loudgate_broadcast_sample.wav",
            source_kind="sample",
            set_active=True,
        )
        return rec.to_summary()

    def load_workspace_source(self, rel_path: str) -> dict[str, Any]:
        src = self.resolve_workspace_file(rel_path)
        copy_name = f"ws_{uuid.uuid4().hex[:12]}{src.suffix.lower()}"
        dst = self.assets_dir / copy_name
        shutil.copy2(src, dst)
        rec = self._register_asset(
            path=dst,
            label=src.name,
            filename=src.name,
            source_kind="workspace",
            set_active=True,
        )
        return rec.to_summary()

    def store_uploaded_audio(self, filename: str, raw_bytes: bytes) -> dict[str, Any]:
        if not raw_bytes:
            raise StudioError(HTTPStatus.BAD_REQUEST, "empty_upload", "Uploaded audio payload is empty.")
        if len(raw_bytes) > self.max_upload_bytes:
            raise StudioError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "file_too_large",
                f"Upload exceeds {self.max_upload_bytes} byte limit.",
            )

        safe_name = _sanitize_filename(filename, fallback="upload.wav")
        ext = Path(safe_name).suffix.lower()
        if ext not in ALLOWED_AUDIO_EXTENSIONS:
            raise StudioError(
                HTTPStatus.BAD_REQUEST,
                "unsupported_extension",
                f"Unsupported audio extension '{ext}'. Allowed: {', '.join(sorted(ALLOWED_AUDIO_EXTENSIONS))}",
            )
        if not _matches_audio_magic(raw_bytes[:64], ext):
            raise StudioError(
                HTTPStatus.BAD_REQUEST,
                "invalid_magic_bytes",
                f"Uploaded file content does not match valid {ext} audio header bytes.",
            )

        dst = self.assets_dir / f"up_{uuid.uuid4().hex[:12]}{ext}"
        dst.write_bytes(raw_bytes)
        try:
            rec = self._register_asset(
                path=dst,
                label=safe_name,
                filename=safe_name,
                source_kind="upload",
                set_active=True,
            )
        except Exception as exc:
            dst.unlink(missing_ok=True)
            raise StudioError(
                HTTPStatus.BAD_REQUEST,
                "decode_failed",
                f"Uploaded audio could not be decoded cleanly: {exc}",
            ) from exc
        return rec.to_summary()

    def run_probe(self, asset_id: str, clip_threshold: float = 0.999) -> dict[str, Any]:
        if not (0.01 <= clip_threshold <= 1.0):
            raise StudioError(
                HTTPStatus.BAD_REQUEST,
                "validation_error",
                "clip_threshold must be between 0.01 and 1.0.",
            )
        rec = self.get_asset(asset_id)
        report = probe_file(rec.path, clip_threshold=clip_threshold)
        with self._lock:
            rec.probe = report
        return rec.to_summary()

    def run_clean(self, params: dict[str, Any]) -> dict[str, Any]:
        asset_id = str(params.get("asset_id") or "")
        src_rec = self.get_asset(asset_id)

        try:
            highpass_hz = float(params.get("highpass_hz", 80.0))
            denoise_strength = float(params.get("denoise_strength", 1.0))
            spectral_floor_db = float(params.get("spectral_floor_db", -28.0))
            n_fft = int(params.get("n_fft", 2048))
            hop_length = int(params.get("hop_length", 512))
        except (TypeError, ValueError) as exc:
            raise StudioError(
                HTTPStatus.BAD_REQUEST, "validation_error", f"Invalid numeric clean parameter: {exc}"
            ) from exc

        if not (0.0 <= highpass_hz <= 2000.0):
            raise StudioError(HTTPStatus.BAD_REQUEST, "validation_error", "highpass_hz must be between 0 and 2000 Hz.")
        if not (0.0 <= denoise_strength <= 5.0):
            raise StudioError(
                HTTPStatus.BAD_REQUEST, "validation_error", "denoise_strength must be between 0.0 and 5.0."
            )
        if not (-90.0 <= spectral_floor_db <= 0.0):
            raise StudioError(
                HTTPStatus.BAD_REQUEST, "validation_error", "spectral_floor_db must be between -90 and 0 dB."
            )
        if n_fft not in {256, 512, 1024, 2048, 4096, 8192}:
            raise StudioError(
                HTTPStatus.BAD_REQUEST,
                "validation_error",
                "n_fft must be one of 256, 512, 1024, 2048, 4096, 8192.",
            )
        if hop_length < 1 or n_fft % hop_length != 0 or (n_fft // hop_length) < 2:
            raise StudioError(
                HTTPStatus.BAD_REQUEST,
                "validation_error",
                f"n_fft ({n_fft}) must be an integer multiple >= 2 of hop_length ({hop_length}) for COLA.",
            )

        cfg = CleanConfig(
            highpass_hz=highpass_hz,
            remove_dc=bool(params.get("remove_dc", True)),
            declick=bool(params.get("declick", True)),
            denoise_strength=denoise_strength,
            spectral_floor_db=spectral_floor_db,
            n_fft=n_fft,
            hop_length=hop_length,
            deess_enabled=bool(params.get("deess_enabled", False)),
            gate_enabled=bool(params.get("gate_enabled", False)),
        )

        stem = Path(src_rec.filename).stem
        out_name = f"{stem}_clean.wav"
        out_path = self.assets_dir / f"clean_{uuid.uuid4().hex[:12]}.wav"
        stats = clean_file(src_rec.path, out_path, config=cfg)
        out_rec = self._register_asset(
            path=out_path,
            label=out_name,
            filename=out_name,
            source_kind="clean",
            set_active=False,
        )

        payload = {
            "before_probe": src_rec.probe.to_dict(),
            "after_probe": out_rec.probe.to_dict(),
            "stats": asdict(stats),
            "output_asset": out_rec.to_summary(),
        }
        with self._lock:
            self._state["clean"] = payload
        return payload

    def run_master(self, params: dict[str, Any]) -> dict[str, Any]:
        asset_id = str(params.get("asset_id") or "")
        src_rec = self.get_asset(asset_id)

        preset = str(params.get("preset", "podcast")).lower().strip()
        if preset not in MASTER_PRESETS:
            raise StudioError(
                HTTPStatus.BAD_REQUEST,
                "validation_error",
                f"Unknown preset '{preset}'. Allowed: {', '.join(sorted(MASTER_PRESETS))}",
            )

        raw_lufs = params.get("target_lufs")
        raw_lra = params.get("target_lra")
        try:
            target_lufs = float(raw_lufs) if raw_lufs not in (None, "") else None
            target_lra = float(raw_lra) if raw_lra not in (None, "") else None
            max_tp = float(params.get("max_true_peak_dbtp", -1.0))
        except (TypeError, ValueError) as exc:
            raise StudioError(
                HTTPStatus.BAD_REQUEST, "validation_error", f"Invalid numeric master parameter: {exc}"
            ) from exc

        if target_lufs is not None and not (-50.0 <= target_lufs <= -3.0):
            raise StudioError(
                HTTPStatus.BAD_REQUEST, "validation_error", "target_lufs must be between -50 and -3 LUFS."
            )
        if not (-12.0 <= max_tp <= 0.0):
            raise StudioError(
                HTTPStatus.BAD_REQUEST, "validation_error", "max_true_peak_dbtp must be between -12 and 0 dBTP."
            )
        if target_lra is not None and not (1.0 <= target_lra <= 30.0):
            raise StudioError(HTTPStatus.BAD_REQUEST, "validation_error", "target_lra must be between 1 and 30 LU.")

        fmt = str(params.get("format", "wav")).lower().lstrip(".")
        if fmt not in ALLOWED_MASTER_FORMATS:
            raise StudioError(
                HTTPStatus.BAD_REQUEST,
                "validation_error",
                f"Unsupported output format '{fmt}'. Allowed: {', '.join(sorted(ALLOWED_MASTER_FORMATS))}",
            )
        bitrate = str(params.get("bitrate", "192k")).strip()
        if not BITRATE_RE.match(bitrate):
            raise StudioError(HTTPStatus.BAD_REQUEST, "validation_error", "bitrate must match e.g. '192k'.")

        cfg = MasterConfig(
            preset=preset,
            target_lufs=target_lufs,
            max_true_peak_dbtp=max_tp,
            target_lra=target_lra,
            bitrate=bitrate,
            dual_mono=bool(params.get("dual_mono", False)),
        )

        stem = Path(src_rec.filename).stem
        out_name = f"{stem}_{preset}.{fmt}"
        out_path = self.assets_dir / f"master_{uuid.uuid4().hex[:12]}.{fmt}"
        res = master_file(src_rec.path, out_path, config=cfg)
        out_rec = self._register_asset(
            path=out_path,
            label=out_name,
            filename=out_name,
            source_kind="master",
            set_active=False,
        )

        payload = {
            "before_probe": src_rec.probe.to_dict(),
            "after_probe": out_rec.probe.to_dict(),
            "master_result": res.to_dict(),
            "output_asset": out_rec.to_summary(),
        }
        with self._lock:
            self._state["master"] = payload
        return payload

    def run_cut(self, params: dict[str, Any]) -> dict[str, Any]:
        asset_id = str(params.get("asset_id") or "")
        src_rec = self.get_asset(asset_id)

        try:
            silence_thresh_db = float(params.get("silence_thresh_db", -40.0))
            min_silence_ms = float(params.get("min_silence_ms", 400.0))
            min_speech_ms = float(params.get("min_speech_ms", 150.0))
            pad_ms = float(params.get("pad_ms", 80.0))
            fps = float(params.get("fps", 25.0))
        except (TypeError, ValueError) as exc:
            raise StudioError(
                HTTPStatus.BAD_REQUEST, "validation_error", f"Invalid numeric cut parameter: {exc}"
            ) from exc

        if not (-90.0 <= silence_thresh_db <= 0.0):
            raise StudioError(
                HTTPStatus.BAD_REQUEST, "validation_error", "silence_thresh_db must be between -90 and 0 dBFS."
            )
        if not (10.0 <= min_silence_ms <= 10000.0):
            raise StudioError(
                HTTPStatus.BAD_REQUEST, "validation_error", "min_silence_ms must be between 10 and 10000 ms."
            )
        if not (10.0 <= min_speech_ms <= 10000.0):
            raise StudioError(
                HTTPStatus.BAD_REQUEST, "validation_error", "min_speech_ms must be between 10 and 10000 ms."
            )
        if not (0.0 <= pad_ms <= 2000.0):
            raise StudioError(HTTPStatus.BAD_REQUEST, "validation_error", "pad_ms must be between 0 and 2000 ms.")
        if not (1.0 <= fps <= 120.0):
            raise StudioError(HTTPStatus.BAD_REQUEST, "validation_error", "fps must be between 1 and 120.")

        render_audio = bool(params.get("render_audio", True))
        cfg = CutConfig(
            silence_thresh_db=silence_thresh_db,
            min_silence_ms=min_silence_ms,
            min_speech_ms=min_speech_ms,
            pad_ms=pad_ms,
            fps=fps,
        )

        export_id = uuid.uuid4().hex[:20]
        export_dir = self.exports_dir / export_id
        export_dir.mkdir(parents=True, exist_ok=True)
        res = cut_silences(src_rec.path, export_dir, config=cfg, render_audio=render_audio)

        # Rename output files cleanly with source stem for user download ergonomics.
        orig_stem = Path(src_rec.filename).stem
        files: dict[str, Path] = {}
        for kind in ("otio", "edl", "srt", "vtt"):
            generated = Path(res[f"{kind}_path"])
            named = export_dir / f"{orig_stem}.{kind}"
            if generated != named:
                shutil.move(str(generated), str(named))
            files[kind] = named

        trimmed_asset_summary: dict[str, Any] | None = None
        if res["trimmed_audio_path"]:
            gen_wav = Path(res["trimmed_audio_path"])
            named_wav = export_dir / f"{orig_stem}_cut.wav"
            if gen_wav != named_wav:
                shutil.move(str(gen_wav), str(named_wav))
            files["wav"] = named_wav
            trimmed_rec = self._register_asset(
                path=named_wav,
                label=named_wav.name,
                filename=named_wav.name,
                source_kind="cut",
                set_active=False,
            )
            trimmed_asset_summary = trimmed_rec.to_summary()

        bundle = ExportBundle(export_id=export_id, stem=orig_stem, files=files)
        with self._lock:
            self._exports[export_id] = bundle

        downloads = {kind: f"/api/exports/{export_id}/{kind}" for kind in files}
        payload = {
            "export_id": export_id,
            "input_duration_s": res["input_duration_s"],
            "kept_duration_s": res["kept_duration_s"],
            "removed_duration_s": res["removed_duration_s"],
            "segment_count": res["segment_count"],
            "segments": res["segments"],
            "silence_thresh_db": silence_thresh_db,
            "downloads": downloads,
            "trimmed_asset": trimmed_asset_summary,
        }
        with self._lock:
            self._state["cut"] = payload
        return payload


class StudioHTTPServer(ThreadingHTTPServer):
    """Loopback multi-threaded HTTP server bound to a StudioSession."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], session: StudioSession) -> None:
        self.session = session
        super().__init__(server_address, StudioRequestHandler)

    def close_studio(self) -> None:
        self.server_close()
        self.session.close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class StudioRequestHandler(BaseHTTPRequestHandler):
    """HTTP request dispatcher enforcing CSP, loopback confinement, and clean JSON APIs."""

    server: StudioHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_common_headers(self, content_type: str, length: int, extra: dict[str, str] | None = None) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Security-Policy", CSP_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._send_common_headers(
            "application/json; charset=utf-8",
            len(body),
            extra={"Cache-Control": "no-store"},
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error_json(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"error": message, "code": code})

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise StudioError(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Invalid Content-Length.") from exc
        if length < 0 or length > MAX_JSON_BODY_BYTES:
            raise StudioError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large", "JSON request too large.")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise StudioError(HTTPStatus.BAD_REQUEST, "invalid_json", f"Invalid JSON payload: {exc}") from exc
        if not isinstance(data, dict):
            raise StudioError(HTTPStatus.BAD_REQUEST, "invalid_json", "JSON payload must be an object.")
        return data

    def _read_upload_payload(self) -> tuple[str, bytes]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise StudioError(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Invalid Content-Length.") from exc
        max_allowed = self.server.session.max_upload_bytes + 65536
        if length <= 0:
            raise StudioError(HTTPStatus.BAD_REQUEST, "empty_upload", "Empty upload body.")
        if length > max_allowed:
            raise StudioError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "file_too_large",
                f"Upload exceeds {self.server.session.max_upload_bytes} byte limit.",
            )

        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" in ctype.lower():
            mime_header = f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1", errors="replace")
            msg = BytesParser(policy=email_default_policy).parsebytes(mime_header + body)
            if not msg.is_multipart():
                raise StudioError(HTTPStatus.BAD_REQUEST, "invalid_multipart", "Malformed multipart/form-data request.")
            for part in msg.iter_parts():
                if part.get_param("name", header="Content-Disposition") == "file":
                    fname = part.get_filename() or "upload.wav"
                    data = part.get_payload(decode=True) or b""
                    return fname, data
            raise StudioError(HTTPStatus.BAD_REQUEST, "missing_file_field", "Missing 'file' field in multipart upload.")

        raw_name = self.headers.get("X-Filename") or "upload.wav"
        return raw_name, body

    def _serve_static(self, rel_path: str) -> None:
        decoded = unquote(rel_path)
        if "\x00" in decoded or "\\" in decoded or ".." in decoded.split("/"):
            self._send_error_json(HTTPStatus.FORBIDDEN, "path_traversal", "Static path traversal is forbidden.")
            return

        cleaned = decoded.lstrip("/")
        if cleaned.startswith("static/"):
            cleaned = cleaned[len("static/") :]
        if cleaned == "":
            cleaned = "index.html"

        static_root = os.path.realpath(str(STATIC_DIR))
        candidate_real = os.path.realpath(os.path.join(static_root, *cleaned.split("/")))
        try:
            common = os.path.commonpath([static_root, candidate_real])
        except ValueError:
            self._send_error_json(HTTPStatus.FORBIDDEN, "path_traversal", "Invalid static path.")
            return

        if common != static_root or candidate_real == static_root:
            self._send_error_json(HTTPStatus.FORBIDDEN, "path_traversal", "Static path escapes asset directory.")
            return

        fpath = Path(candidate_real)
        if not fpath.exists() or not fpath.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", f"Static asset not found: {cleaned}")
            return

        data = fpath.read_bytes()
        ctype = CONTENT_TYPES.get(fpath.suffix.lower(), "application/octet-stream")
        self.send_response(HTTPStatus.OK)
        self._send_common_headers(ctype, len(data), extra={"Cache-Control": "no-cache"})
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _serve_file_stream(self, fpath: Path, download_name: str | None = None) -> None:
        total = fpath.stat().st_size
        ctype = CONTENT_TYPES.get(fpath.suffix.lower(), "application/octet-stream")
        extra: dict[str, str] = {"Cache-Control": "no-store", "Accept-Ranges": "bytes"}
        if download_name:
            safe_dl = _sanitize_filename(download_name, fallback=fpath.name)
            extra["Content-Disposition"] = f'attachment; filename="{safe_dl}"'

        range_hdr = self.headers.get("Range")
        if range_hdr and not download_name:
            m = RANGE_RE.match(range_hdr.strip())
            if not m:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self._send_common_headers("text/plain; charset=utf-8", 0, extra={"Content-Range": f"bytes */{total}"})
                self.end_headers()
                return
            s_raw, e_raw = m.groups()
            start = int(s_raw) if s_raw else 0
            end = int(e_raw) if e_raw else max(0, total - 1)
            if total == 0 or start >= total or end < start:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self._send_common_headers("text/plain; charset=utf-8", 0, extra={"Content-Range": f"bytes */{total}"})
                self.end_headers()
                return
            end = min(end, total - 1)
            chunk_len = end - start + 1
            extra["Content-Range"] = f"bytes {start}-{end}/{total}"
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self._send_common_headers(ctype, chunk_len, extra=extra)
            self.end_headers()
            if self.command != "HEAD":
                with fpath.open("rb") as fh:
                    fh.seek(start)
                    self.wfile.write(fh.read(chunk_len))
            return

        self.send_response(HTTPStatus.OK)
        self._send_common_headers(ctype, total, extra=extra)
        self.end_headers()
        if self.command != "HEAD":
            with fpath.open("rb") as fh:
                while True:
                    buf = fh.read(65536)
                    if not buf:
                        break
                    self.wfile.write(buf)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        raw_path = parsed.path
        decoded = unquote(raw_path)

        try:
            if ".." in decoded.split("/") or "\\" in decoded or "\x00" in decoded:
                raise StudioError(HTTPStatus.FORBIDDEN, "path_traversal", "Path traversal is forbidden.")

            if decoded == "/api/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "version": __version__,
                        "workspace_dir": str(self.server.session.workspace_dir),
                        "presets": MASTER_PRESETS,
                        "limits": {
                            "max_upload_bytes": self.server.session.max_upload_bytes,
                        },
                    },
                )
                return

            if decoded == "/api/session":
                self._send_json(HTTPStatus.OK, self.server.session.get_session_snapshot())
                return

            if decoded == "/api/workspace/files":
                files = self.server.session.list_workspace_files()
                self._send_json(HTTPStatus.OK, {"files": files})
                return

            m_asset = re.match(r"^/api/assets/([^/]+)/(audio|download)$", decoded)
            if m_asset:
                asset_id, mode = m_asset.groups()
                rec = self.server.session.get_asset(asset_id)
                self._serve_file_stream(rec.path, download_name=rec.filename if mode == "download" else None)
                return

            m_export = re.match(r"^/api/exports/([^/]+)/([a-z0-9]+)$", decoded)
            if m_export:
                export_id, kind = m_export.groups()
                fpath, dl_name = self.server.session.get_export_file(export_id, kind)
                self._serve_file_stream(fpath, download_name=dl_name)
                return

            if decoded.startswith("/api/"):
                raise StudioError(HTTPStatus.NOT_FOUND, "not_found", f"Unknown API endpoint: {decoded}")

            self._serve_static(raw_path)
        except StudioError as err:
            self._send_error_json(err.status, err.code, err.message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as exc:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", str(exc))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        decoded = unquote(parsed.path)

        try:
            if ".." in decoded.split("/") or "\\" in decoded or "\x00" in decoded:
                raise StudioError(HTTPStatus.FORBIDDEN, "path_traversal", "Path traversal is forbidden.")

            if decoded == "/api/upload":
                fname, data = self._read_upload_payload()
                res = self.server.session.store_uploaded_audio(fname, data)
                self._send_json(HTTPStatus.OK, res)
                return

            body = self._read_json_body()

            if decoded == "/api/sample":
                self._send_json(HTTPStatus.OK, self.server.session.load_built_in_sample())
                return

            if decoded == "/api/session/reset":
                self._send_json(HTTPStatus.OK, self.server.session.reset_session())
                return

            if decoded == "/api/session/activate":
                asset_id = str(body.get("asset_id") or "")
                self._send_json(HTTPStatus.OK, self.server.session.set_active_source(asset_id))
                return

            if decoded == "/api/workspace/load":
                rel_path = body.get("path")
                if not isinstance(rel_path, str):
                    raise StudioError(HTTPStatus.BAD_REQUEST, "invalid_path", "Expected string 'path'.")
                self._send_json(HTTPStatus.OK, self.server.session.load_workspace_source(rel_path))
                return

            if decoded == "/api/probe":
                asset_id = str(body.get("asset_id") or "")
                try:
                    clip_thresh = float(body.get("clip_threshold", 0.999))
                except (TypeError, ValueError) as exc:
                    raise StudioError(
                        HTTPStatus.BAD_REQUEST, "validation_error", "Invalid clip_threshold."
                    ) from exc
                self._send_json(HTTPStatus.OK, self.server.session.run_probe(asset_id, clip_threshold=clip_thresh))
                return

            if decoded == "/api/clean":
                self._send_json(HTTPStatus.OK, self.server.session.run_clean(body))
                return

            if decoded == "/api/master":
                self._send_json(HTTPStatus.OK, self.server.session.run_master(body))
                return

            if decoded == "/api/cut":
                self._send_json(HTTPStatus.OK, self.server.session.run_cut(body))
                return

            raise StudioError(HTTPStatus.NOT_FOUND, "not_found", f"Unknown API endpoint: {decoded}")
        except StudioError as err:
            self._send_error_json(err.status, err.code, err.message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as exc:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", str(exc))


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    workspace_dir: str | Path = ".",
    max_upload_bytes: int = MAX_UPLOAD_BYTES,
) -> StudioHTTPServer:
    """Create a configured loopback StudioHTTPServer without starting the accept loop."""
    safe_host = validate_loopback_host(host)
    session = StudioSession(workspace_dir=workspace_dir, max_upload_bytes=max_upload_bytes)
    return StudioHTTPServer((safe_host, port), session)


def find_desktop_browser() -> str | None:
    """Locate an installed Edge or Chrome binary supporting standalone --app windows."""
    candidates: list[str] = []
    for name in ("msedge", "microsoft-edge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        resolved = shutil.which(name)
        if resolved:
            candidates.append(resolved)

    if sys.platform == "win32":
        program_dirs = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        for base in program_dirs:
            if not base:
                continue
            candidates.extend(
                [
                    os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                    os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                ]
            )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ]
        )

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _launch_desktop_window(url: str, server: StudioHTTPServer) -> None:
    """Open standalone desktop app window via pywebview or Chromium --app fallback."""
    try:
        import webview  # type: ignore[import-not-found]
    except ImportError:
        webview = None

    if webview is not None:
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()
        try:
            webview.create_window("loudgate Studio", url, width=1440, height=900, min_size=(390, 720))
            webview.start()
        finally:
            server.shutdown()
        return

    browser_bin = find_desktop_browser()
    if browser_bin:
        app_profile = server.session.session_dir / "app_profile"
        app_profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [
                browser_bin,
                f"--app={url}",
                "--window-size=1440,900",
                f"--user-data-dir={app_profile}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def serve_ui(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    workspace_dir: str | Path = ".",
    open_browser: bool = True,
    desktop: bool = False,
) -> None:
    """Start the local loudgate studio GUI server and block until interrupted."""
    server = create_server(host=host, port=port, workspace_dir=workspace_dir)
    bound_host, bound_port = server.server_address[:2]
    url = f"http://{bound_host}:{bound_port}/"
    print(f"loudgate Studio UI running at {url} (workspace: {server.session.workspace_dir})")
    print("Press Ctrl+C to stop.")
    try:
        if desktop:
            _launch_desktop_window(url, server)
            return
        if open_browser:
            threading.Timer(0.25, lambda: webbrowser.open(url)).start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close_studio()


def build_ui_parser() -> argparse.ArgumentParser:
    """Build argument parser for standalone loudgate-ui entrypoint."""
    parser = argparse.ArgumentParser(
        prog="loudgate-ui",
        description="Launch loudgate local broadcast audio studio GUI (loopback HTTP).",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Loopback bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port (default: 8742; use 0 for auto).")
    parser.add_argument("--workspace-dir", default=".", help="Workspace directory confined for local audio selection.")
    parser.add_argument("--no-open", action="store_true", help="Do not automatically open the browser.")
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="Launch in standalone desktop app window mode (pywebview or Edge/Chrome --app).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for loudgate-ui."""
    parser = build_ui_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
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
