const SPEC_PALETTE = (() => {
  // iZotope RX thermal/obsidian-to-amber LUT (256 x RGBA)
  const lut = new Uint8ClampedArray(256 * 4);
  const stops = [
    [0.0, 22, 24, 28],
    [0.25, 22, 42, 74],
    [0.5, 118, 42, 18],
    [0.75, 217, 119, 6],
    [0.92, 250, 204, 21],
    [1.0, 254, 249, 195],
  ];
  for (let i = 0; i < 256; i++) {
    const p = i / 255;
    let s0 = stops[0];
    let s1 = stops[stops.length - 1];
    for (let k = 0; k < stops.length - 1; k++) {
      if (p >= stops[k][0] && p <= stops[k + 1][0]) {
        s0 = stops[k];
        s1 = stops[k + 1];
        break;
      }
    }
    const span = Math.max(1e-6, s1[0] - s0[0]);
    const f = (p - s0[0]) / span;
    lut[i * 4 + 0] = Math.round(s0[1] + (s1[1] - s0[1]) * f);
    lut[i * 4 + 1] = Math.round(s0[2] + (s1[2] - s0[2]) * f);
    lut[i * 4 + 2] = Math.round(s0[3] + (s1[3] - s0[3]) * f);
    lut[i * 4 + 3] = 255;
  }
  return lut;
})();

function decodeBase64Bytes(b64) {
  if (!b64) return null;
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) {
    out[i] = bin.charCodeAt(i);
  }
  return out;
}

export class WaveformRenderer {
  constructor(canvas, onSeek, onHover, onThresholdDrag) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.onSeek = onSeek;
    this.onHover = onHover;
    this.onThresholdDrag = onThresholdDrag;
    this.waveform = null;
    this.specCanvas = null;
    this.playheadRatio = 0;
    this.cutData = null;
    this.threshDb = -40;
    this.showThreshold = false;
    this.viewMode = "dual"; // "dual" | "spec" | "wave"
    this.emptyMessage = "";
    this.dragMode = null; // "seek" | "thresh"
    this.waveBounds = { top: 130, height: 130, scaleY: 50, midY: 195 };

    this._bindEvents();
    this.resize();
  }

  _waveYToDb(clientY) {
    const rect = this.canvas.getBoundingClientRect();
    const y = clientY - rect.top;
    const { midY, scaleY } = this.waveBounds;
    const amp = Math.max(0.0003, Math.min(1.0, Math.abs(y - midY) / Math.max(8, scaleY)));
    return Math.max(-70, Math.min(-5, Math.round(20 * Math.log10(amp))));
  }

  _isNearThresholdLine(clientX, clientY) {
    if (!this.showThreshold || this.viewMode === "spec" || !this.waveform) return false;
    const rect = this.canvas.getBoundingClientRect();
    const x = clientX - rect.left;
    const y = clientY - rect.top;
    const { top, height, midY, scaleY } = this.waveBounds;
    if (y < top || y > top + height) return false;
    const threshAmp = Math.pow(10, this.threshDb / 20);
    const dy = Math.min(height * 0.47, Math.max(2, threshAmp * scaleY));
    const onHandle = x >= rect.width - 96 && Math.abs(y - (midY - dy)) <= 10;
    const onLine = dy >= 6 && (Math.abs(y - (midY - dy)) <= 4 || Math.abs(y - (midY + dy)) <= 4);
    return onHandle || onLine;
  }

  _bindEvents() {
    const getRatioFromEvent = (evt) => {
      const rect = this.canvas.getBoundingClientRect();
      if (rect.width <= 0) return 0;
      const x = Math.max(0, Math.min(rect.width, evt.clientX - rect.left));
      return x / rect.width;
    };

    this.canvas.addEventListener("pointerdown", (evt) => {
      if (!this.waveform || !this.waveform.duration_s) return;
      this.canvas.setPointerCapture(evt.pointerId);
      if (this._isNearThresholdLine(evt.clientX, evt.clientY)) {
        this.dragMode = "thresh";
        const newDb = this._waveYToDb(evt.clientY);
        this.threshDb = newDb;
        if (this.onThresholdDrag) this.onThresholdDrag(newDb);
        this.draw();
        return;
      }
      this.dragMode = "seek";
      this.onSeek(getRatioFromEvent(evt) * this.waveform.duration_s);
    });

    this.canvas.addEventListener("pointermove", (evt) => {
      if (!this.waveform || !this.waveform.duration_s) return;
      const rect = this.canvas.getBoundingClientRect();
      const y = evt.clientY - rect.top;

      if (this.dragMode === "thresh") {
        const newDb = this._waveYToDb(evt.clientY);
        this.threshDb = newDb;
        if (this.onThresholdDrag) this.onThresholdDrag(newDb);
        this.draw();
        return;
      }

      if (this.onHover) {
        const ratio = getRatioFromEvent(evt);
        const timeS = ratio * this.waveform.duration_s;
        let freqHz = null;
        if (this.viewMode !== "wave" && this.waveform.spectrogram) {
          const specH = this.viewMode === "spec" ? (this.height || 260) : Math.floor((this.height || 260) * 0.52);
          if (y >= 0 && y <= specH) {
            const normY = 1 - Math.max(0, Math.min(1, y / Math.max(1, specH)));
            const fMin = this.waveform.spectrogram.f_min_hz || 40;
            const fMax = this.waveform.spectrogram.f_max_hz || 20000;
            freqHz = fMin * Math.pow(fMax / fMin, normY);
          }
        }
        this.onHover(timeS, freqHz);
      }

      this.canvas.style.cursor = this._isNearThresholdLine(evt.clientX, evt.clientY) ? "ns-resize" : "crosshair";
      if (this.dragMode === "seek") {
        this.onSeek(getRatioFromEvent(evt) * this.waveform.duration_s);
      }
    });

    const stopDrag = () => {
      this.dragMode = null;
    };
    this.canvas.addEventListener("pointerup", stopDrag);
    this.canvas.addEventListener("pointercancel", stopDrag);

    if (typeof ResizeObserver !== "undefined") {
      this._ro = new ResizeObserver(() => this.resize());
      this._ro.observe(this.canvas.parentElement);
    } else {
      window.addEventListener("resize", () => this.resize());
    }
  }

  resize() {
    const wrap = this.canvas.parentElement;
    if (!wrap) return;
    const width = Math.max(240, Math.floor(wrap.clientWidth));
    const height = window.innerWidth <= 600 ? 210 : 260;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = Math.floor(width * dpr);
    this.canvas.height = Math.floor(height * dpr);
    this.canvas.style.height = `${height}px`;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.width = width;
    this.height = height;
    this.draw();
  }

  _buildSpectrogramSurface(spec) {
    if (!spec || !spec.cols || !spec.rows || !spec.data_b64) {
      this.specCanvas = null;
      return;
    }
    const bytes = decodeBase64Bytes(spec.data_b64);
    if (!bytes || bytes.length < spec.cols * spec.rows) {
      this.specCanvas = null;
      return;
    }
    const off = document.createElement("canvas");
    off.width = spec.cols;
    off.height = spec.rows;
    const octx = off.getContext("2d");
    const img = octx.createImageData(spec.cols, spec.rows);
    const dst = img.data;
    for (let i = 0; i < spec.cols * spec.rows; i++) {
      const idx = bytes[i] * 4;
      dst[i * 4 + 0] = SPEC_PALETTE[idx + 0];
      dst[i * 4 + 1] = SPEC_PALETTE[idx + 1];
      dst[i * 4 + 2] = SPEC_PALETTE[idx + 2];
      dst[i * 4 + 3] = 255;
    }
    octx.putImageData(img, 0, 0);
    this.specCanvas = off;
  }

  setViewMode(mode) {
    this.viewMode = mode;
    this.draw();
  }

  setThresholdDb(threshDb) {
    this.threshDb = Number(threshDb);
    this.draw();
  }

  setData({ waveform, cutData = null, threshDb = -40, showThreshold = false, playheadRatio = 0, emptyMessage = "" }) {
    const wfChanged = this.waveform !== waveform;
    this.waveform = waveform || null;
    this.cutData = cutData || null;
    this.threshDb = Number(threshDb);
    this.showThreshold = Boolean(showThreshold);
    this.playheadRatio = Math.max(0, Math.min(1, playheadRatio || 0));
    this.emptyMessage = emptyMessage;
    if (wfChanged) {
      this._buildSpectrogramSurface(waveform ? waveform.spectrogram : null);
    }
    this.draw();
  }

  setPlayhead(currentTimeS) {
    if (!this.waveform || !this.waveform.duration_s) {
      this.playheadRatio = 0;
    } else {
      this.playheadRatio = Math.max(0, Math.min(1, currentTimeS / this.waveform.duration_s));
    }
    this.draw();
  }

  _drawSpectrogramLayer(ctx, w, top, height) {
    ctx.fillStyle = "#16181c";
    ctx.fillRect(0, top, w, height);

    if (this.specCanvas) {
      ctx.save();
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(this.specCanvas, 0, top, w, height);
      ctx.restore();
    }

    // Frequency scale ticks on left gutter
    const fMin = (this.waveform.spectrogram && this.waveform.spectrogram.f_min_hz) || 40;
    const fMax = (this.waveform.spectrogram && this.waveform.spectrogram.f_max_hz) || 20000;
    const ticks = [16000, 5000, 1000, 200, 50].filter((f) => f >= fMin && f <= fMax);
    ctx.font = "10px ui-monospace, monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    for (const freq of ticks) {
      const norm = Math.log(freq / fMin) / Math.log(fMax / fMin);
      const y = top + height * (1 - norm);
      ctx.strokeStyle = "rgba(255, 255, 255, 0.08)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, Math.round(y) + 0.5);
      ctx.lineTo(w, Math.round(y) + 0.5);
      ctx.stroke();

      const lbl = freq >= 1000 ? `${Math.round(freq / 1000)}k` : `${freq}`;
      ctx.fillStyle = "rgba(16, 18, 22, 0.72)";
      ctx.fillRect(3, y - 6, 26, 12);
      ctx.fillStyle = "#cbd5e1";
      ctx.fillText(lbl, 5, y);
    }
  }

  _drawWaveformLayer(ctx, w, top, height) {
    const midY = top + height / 2;
    ctx.fillStyle = "#16181c";
    ctx.fillRect(0, top, w, height);

    // Horizontal reference grid
    ctx.strokeStyle = "rgba(50, 56, 68, 0.5)";
    ctx.lineWidth = 1;
    for (const frac of [0.25, 0.5, 0.75]) {
      const y = Math.round(top + height * frac) + 0.5;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    const totalDur = Math.max(1e-6, this.waveform.duration_s);

    // Keep-Speech vs Trim-Silence regions
    if (this.cutData && Array.isArray(this.cutData.segments)) {
      let cursor = 0;
      for (const seg of this.cutData.segments) {
        if (seg.start_s > cursor) {
          const x0 = (cursor / totalDur) * w;
          const x1 = (seg.start_s / totalDur) * w;
          ctx.fillStyle = "rgba(239, 68, 68, 0.16)";
          ctx.fillRect(x0, top, Math.max(1, x1 - x0), height);
        }
        const sx0 = (seg.start_s / totalDur) * w;
        const sx1 = (seg.end_s / totalDur) * w;
        ctx.fillStyle = "rgba(46, 204, 113, 0.22)";
        ctx.fillRect(sx0, top, Math.max(2, sx1 - sx0), height);
        ctx.fillStyle = "#2ecc71";
        ctx.fillRect(sx0, top, Math.max(2, sx1 - sx0), 2);
        ctx.fillRect(sx0, top + height - 2, Math.max(2, sx1 - sx0), 2);
        if (sx1 - sx0 > 22) {
          ctx.fillStyle = "#86efac";
          ctx.font = "bold 10px ui-monospace, monospace";
          ctx.textAlign = "left";
          ctx.textBaseline = "top";
          ctx.fillText(`#${seg.index}`, sx0 + 3, top + 4);
        }
        cursor = Math.max(cursor, seg.end_s);
      }
      if (cursor < totalDur) {
        const rx0 = (cursor / totalDur) * w;
        ctx.fillStyle = "rgba(239, 68, 68, 0.16)";
        ctx.fillRect(rx0, top, Math.max(1, w - rx0), height);
      }
    }

    const { bins, mins, maxs, rms } = this.waveform;
    const maxAbsPeak = Math.max(0.05, ...mins.map(Math.abs), ...maxs.map(Math.abs));
    const scaleY = (height * 0.44) / Math.min(1.0, Math.max(maxAbsPeak * 1.06, 0.12));
    this.waveBounds = { top, height, scaleY, midY };

    const barW = Math.max(1, w / bins);
    ctx.fillStyle = "rgba(56, 189, 248, 0.42)";
    for (let i = 0; i < bins; i++) {
      const x = (i / bins) * w;
      const yTop = midY - maxs[i] * scaleY;
      const yBot = midY - mins[i] * scaleY;
      ctx.fillRect(x, yTop, Math.ceil(barW), Math.max(1.2, yBot - yTop));
    }

    ctx.fillStyle = "rgba(125, 211, 252, 0.92)";
    for (let i = 0; i < bins; i++) {
      const x = (i / bins) * w;
      const rH = Math.max(1, rms[i] * scaleY * 2);
      ctx.fillRect(x, midY - rH / 2, Math.ceil(barW), rH);
    }

    // Interactive threshold lines (+/- threshDb)
    if (this.showThreshold) {
      const threshAmp = Math.pow(10, this.threshDb / 20);
      const dy = Math.min(height * 0.47, Math.max(2, threshAmp * scaleY));
      ctx.save();
      ctx.strokeStyle = "rgba(245, 158, 11, 0.85)";
      ctx.setLineDash([4, 4]);
      ctx.lineWidth = 1;
      for (const yPos of [midY - dy, midY + dy]) {
        ctx.beginPath();
        ctx.moveTo(0, Math.round(yPos) + 0.5);
        ctx.lineTo(w, Math.round(yPos) + 0.5);
        ctx.stroke();
      }
      ctx.fillStyle = "rgba(245, 158, 11, 0.92)";
      ctx.font = "10px ui-monospace, monospace";
      ctx.textAlign = "right";
      ctx.textBaseline = "bottom";
      ctx.fillText(`THR ${this.threshDb} dB`, w - 6, midY - dy - 2);
      ctx.restore();
    }
  }

  draw() {
    const ctx = this.ctx;
    const w = this.width || 600;
    const h = this.height || 260;

    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#16181c";
    ctx.fillRect(0, 0, w, h);

    if (!this.waveform || !this.waveform.bins) {
      ctx.strokeStyle = "rgba(50, 56, 68, 0.45)";
      ctx.beginPath();
      ctx.moveTo(0, Math.round(h / 2) + 0.5);
      ctx.lineTo(w, Math.round(h / 2) + 0.5);
      ctx.stroke();
      ctx.fillStyle = "#9aa3b2";
      ctx.font = "12px ui-monospace, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(this.emptyMessage || "No audio loaded", w / 2, h / 2, Math.max(120, w - 24));
      return;
    }

    if (this.viewMode === "spec") {
      this._drawSpectrogramLayer(ctx, w, 0, h);
    } else if (this.viewMode === "wave") {
      this._drawWaveformLayer(ctx, w, 0, h);
    } else {
      const specH = Math.floor(h * 0.51);
      const waveTop = specH + 1;
      const waveH = h - waveTop;
      this._drawSpectrogramLayer(ctx, w, 0, specH);
      ctx.fillStyle = "#101216";
      ctx.fillRect(0, specH, w, 1);
      this._drawWaveformLayer(ctx, w, waveTop, waveH);
    }

    // Playhead cursor spanning full canvas height
    const px = Math.round(this.playheadRatio * w) + 0.5;
    ctx.fillStyle = "rgba(56, 189, 248, 0.08)";
    ctx.fillRect(0, 0, px, h);
    ctx.strokeStyle = "#f8fafc";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(px, 0);
    ctx.lineTo(px, h);
    ctx.stroke();
  }
}
