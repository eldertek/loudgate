export class WaveformRenderer {
  constructor(canvas, onSeek, onHover) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.onSeek = onSeek;
    this.onHover = onHover;
    this.waveform = null;
    this.playheadRatio = 0;
    this.cutData = null;
    this.emptyMessage = "";
    this.isDragging = false;

    this._bindEvents();
    this.resize();
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
      this.isDragging = true;
      this.canvas.setPointerCapture(evt.pointerId);
      const ratio = getRatioFromEvent(evt);
      this.onSeek(ratio * this.waveform.duration_s);
    });

    this.canvas.addEventListener("pointermove", (evt) => {
      if (!this.waveform || !this.waveform.duration_s) return;
      const ratio = getRatioFromEvent(evt);
      const timeS = ratio * this.waveform.duration_s;
      if (this.onHover) {
        this.onHover(timeS);
      }
      if (this.isDragging) {
        this.onSeek(timeS);
      }
    });

    const stopDrag = () => {
      this.isDragging = false;
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
    const height = 170;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = Math.floor(width * dpr);
    this.canvas.height = Math.floor(height * dpr);
    this.canvas.style.height = `${height}px`;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.width = width;
    this.height = height;
    this.draw();
  }

  setData({ waveform, cutData = null, playheadRatio = 0, emptyMessage = "" }) {
    this.waveform = waveform || null;
    this.cutData = cutData || null;
    this.playheadRatio = Math.max(0, Math.min(1, playheadRatio || 0));
    this.emptyMessage = emptyMessage;
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

  draw() {
    const ctx = this.ctx;
    const w = this.width || 600;
    const h = this.height || 170;
    const midY = h / 2;

    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#0d1324";
    ctx.fillRect(0, 0, w, h);

    // Subtle horizontal grid lines
    ctx.strokeStyle = "rgba(30, 38, 64, 0.85)";
    ctx.lineWidth = 1;
    for (const frac of [0.25, 0.5, 0.75]) {
      const y = Math.round(h * frac) + 0.5;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    if (!this.waveform || !this.waveform.bins) {
      ctx.fillStyle = "#7281a8";
      ctx.font = "13px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(this.emptyMessage || "No audio loaded", w / 2, midY, Math.max(120, w - 32));
      return;
    }

    const totalDur = Math.max(1e-6, this.waveform.duration_s);

    // Draw cut silence & kept speech overlays if Cut mode active
    if (this.cutData && Array.isArray(this.cutData.segments)) {
      const segs = this.cutData.segments;
      let cursor = 0;
      for (const seg of segs) {
        if (seg.start_s > cursor) {
          const x0 = (cursor / totalDur) * w;
          const x1 = (seg.start_s / totalDur) * w;
          ctx.fillStyle = "rgba(239, 68, 68, 0.14)";
          ctx.fillRect(x0, 0, Math.max(1, x1 - x0), h);
        }
        const sx0 = (seg.start_s / totalDur) * w;
        const sx1 = (seg.end_s / totalDur) * w;
        ctx.fillStyle = "rgba(34, 197, 94, 0.18)";
        ctx.fillRect(sx0, 0, Math.max(2, sx1 - sx0), h);
        ctx.fillStyle = "#22c55e";
        ctx.fillRect(sx0, 0, Math.max(2, sx1 - sx0), 3);
        ctx.fillRect(sx0, h - 3, Math.max(2, sx1 - sx0), 3);

        if (sx1 - sx0 > 24) {
          ctx.fillStyle = "#86efac";
          ctx.font = "bold 10px ui-monospace, monospace";
          ctx.textAlign = "left";
          ctx.textBaseline = "top";
          ctx.fillText(`#${seg.index}`, sx0 + 4, 6);
        }
        cursor = Math.max(cursor, seg.end_s);
      }
      if (cursor < totalDur) {
        const rx0 = (cursor / totalDur) * w;
        ctx.fillStyle = "rgba(239, 68, 68, 0.14)";
        ctx.fillRect(rx0, 0, Math.max(1, w - rx0), h);
      }
    }

    const { bins, mins, maxs, rms } = this.waveform;
    const maxAbsPeak = Math.max(0.05, ...mins.map(Math.abs), ...maxs.map(Math.abs));
    const scaleY = (h * 0.44) / Math.min(1.0, Math.max(maxAbsPeak * 1.08, 0.12));

    // Peak envelope bars
    const barW = Math.max(1, w / bins);
    ctx.fillStyle = "rgba(79, 109, 245, 0.46)";
    for (let i = 0; i < bins; i++) {
      const x = (i / bins) * w;
      const yTop = midY - maxs[i] * scaleY;
      const yBot = midY - mins[i] * scaleY;
      const barH = Math.max(1.5, yBot - yTop);
      ctx.fillRect(x, yTop, Math.ceil(barW), barH);
    }

    // Inner RMS body
    ctx.fillStyle = "rgba(142, 162, 255, 0.9)";
    for (let i = 0; i < bins; i++) {
      const x = (i / bins) * w;
      const rH = Math.max(1, rms[i] * scaleY * 2);
      ctx.fillRect(x, midY - rH / 2, Math.ceil(barW), rH);
    }

    // Threshold horizontal dashed markers when Cut tab active
    if (this.cutData && typeof this.cutData.silence_thresh_db === "number") {
      const threshAmp = Math.pow(10, this.cutData.silence_thresh_db / 20);
      const dy = threshAmp * scaleY;
      if (dy > 2 && dy < h * 0.46) {
        ctx.save();
        ctx.strokeStyle = "rgba(245, 158, 11, 0.75)";
        ctx.setLineDash([4, 4]);
        ctx.lineWidth = 1;
        for (const yPos of [midY - dy, midY + dy]) {
          ctx.beginPath();
          ctx.moveTo(0, yPos);
          ctx.lineTo(w, yPos);
          ctx.stroke();
        }
        ctx.restore();
      }
    }

    // Playhead cursor
    const px = Math.round(this.playheadRatio * w) + 0.5;
    ctx.fillStyle = "rgba(79, 109, 245, 0.12)";
    ctx.fillRect(0, 0, px, h);
    ctx.strokeStyle = "#f8fafc";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(px, 0);
    ctx.lineTo(px, h);
    ctx.stroke();
  }
}
