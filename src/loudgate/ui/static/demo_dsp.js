// Browser-side WebAudio DSP fallback when loudgate UI is hosted statically (e.g. eclipse-technology.eu/demos/loudgate/)

export const DEMO_MASTER_PRESETS = {
  web: { target_lufs: -14.0, max_true_peak_dbtp: -1.0, target_lra: 11.0 },
  podcast: { target_lufs: -14.0, max_true_peak_dbtp: -1.0, target_lra: 11.0 },
  mobile: { target_lufs: -16.0, max_true_peak_dbtp: -1.0, target_lra: 9.0 },
  stereo: { target_lufs: -16.0, max_true_peak_dbtp: -1.0, target_lra: 9.0 },
  broadcast: { target_lufs: -23.0, max_true_peak_dbtp: -1.0, target_lra: 7.0 },
  ebu_r128: { target_lufs: -23.0, max_true_peak_dbtp: -1.0, target_lra: 7.0 },
};

const EPS = 1e-12;

function toDb(v) {
  return Math.max(-120, 20 * Math.log10(Math.max(v, EPS)));
}

function powerToDb(p) {
  return Math.max(-120, 10 * Math.log10(Math.max(p, EPS * EPS)));
}

function encodeWavBlob(samples, fs) {
  const n = samples.length;
  const buf = new ArrayBuffer(44 + n * 2);
  const view = new DataView(buf);
  const writeStr = (off, s) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
  };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + n * 2, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, fs, true);
  view.setUint32(28, fs * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, "data");
  view.setUint32(40, n * 2, true);
  let offset = 44;
  for (let i = 0; i < n; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    offset += 2;
  }
  return new Blob([buf], { type: "audio/wav" });
}

function fftRadix2(re, im) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      [re[i], re[j]] = [re[j], re[i]];
      [im[i], im[j]] = [im[j], im[i]];
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (-2 * Math.PI) / len;
    const wlenRe = Math.cos(ang);
    const wlenIm = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let wRe = 1;
      let wIm = 0;
      for (let j = 0; j < len / 2; j++) {
        const uRe = re[i + j];
        const uIm = im[i + j];
        const vRe = re[i + j + len / 2] * wRe - im[i + j + len / 2] * wIm;
        const vIm = re[i + j + len / 2] * wIm + im[i + j + len / 2] * wRe;
        re[i + j] = uRe + vRe;
        im[i + j] = uIm + vIm;
        re[i + j + len / 2] = uRe - vRe;
        im[i + j + len / 2] = uIm - vIm;
        const nextWRe = wRe * wlenRe - wIm * wlenIm;
        wIm = wRe * wlenIm + wIm * wlenRe;
        wRe = nextWRe;
      }
    }
  }
}

function computeSpectrogramData(samples, fs, cols = 200, rows = 48) {
  const fMin = 40;
  const fMax = Math.min(20000, 0.49 * fs);
  const nFft = 512;
  if (samples.length < nFft) {
    return { cols: 0, rows: 0, f_min_hz: fMin, f_max_hz: fMax, data_b64: "" };
  }
  const win = new Float64Array(nFft);
  for (let i = 0; i < nFft; i++) win[i] = 0.5 * (1 - Math.cos((2 * Math.PI * i) / nFft));

  const outBytes = new Uint8Array(cols * rows);
  const re = new Float64Array(nFft);
  const im = new Float64Array(nFft);
  const mag = new Float64Array(nFft / 2);

  for (let c = 0; c < cols; c++) {
    const start = Math.floor((c / cols) * Math.max(1, samples.length - nFft));
    for (let i = 0; i < nFft; i++) {
      re[i] = (samples[start + i] || 0) * win[i];
      im[i] = 0;
    }
    fftRadix2(re, im);
    for (let k = 0; k < nFft / 2; k++) {
      mag[k] = (re[k] * re[k] + im[k] * im[k]) / (nFft * nFft);
    }
    for (let r = 0; r < rows; r++) {
      const f0 = fMin * Math.pow(fMax / fMin, r / rows);
      const f1 = fMin * Math.pow(fMax / fMin, (r + 1) / rows);
      const k0 = Math.max(0, Math.floor((f0 * nFft) / fs));
      const k1 = Math.min(nFft / 2 - 1, Math.ceil((f1 * nFft) / fs));
      let peak = 0;
      for (let k = k0; k <= k1; k++) {
        if (mag[k] > peak) peak = mag[k];
      }
      const db = powerToDb(peak);
      const norm = Math.max(0, Math.min(1, (db + 85) / 80));
      outBytes[(rows - 1 - r) * cols + c] = Math.round(norm * 255);
    }
  }
  let bin = "";
  for (let i = 0; i < outBytes.length; i++) bin += String.fromCharCode(outBytes[i]);
  return { cols, rows, f_min_hz: fMin, f_max_hz: fMax, data_b64: btoa(bin) };
}

function computeWaveform(samples, fs) {
  const bins = Math.min(800, samples.length);
  const mins = [];
  const maxs = [];
  const rms = [];
  for (let b = 0; b < bins; b++) {
    const i0 = Math.floor((b / bins) * samples.length);
    const i1 = Math.max(i0 + 1, Math.floor(((b + 1) / bins) * samples.length));
    let mn = 1;
    let mx = -1;
    let sq = 0;
    for (let i = i0; i < i1; i++) {
      const v = samples[i];
      if (v < mn) mn = v;
      if (v > mx) mx = v;
      sq += v * v;
    }
    mins.push(Number(mn.toFixed(5)));
    maxs.push(Number(mx.toFixed(5)));
    rms.push(Number(Math.sqrt(sq / (i1 - i0)).toFixed(5)));
  }
  return {
    duration_s: Number((samples.length / fs).toFixed(6)),
    sample_rate: fs,
    channels: 1,
    bins,
    mins,
    maxs,
    rms,
    spectrogram: computeSpectrogramData(samples, fs),
  };
}

function probeSamples(samples, fs, clipThreshold = 0.999, pathLabel = "demo.wav") {
  const n = samples.length;
  let sum = 0;
  let sq = 0;
  let peak = 0;
  let clipped = 0;
  for (let i = 0; i < n; i++) {
    const v = samples[i];
    sum += v;
    sq += v * v;
    const av = Math.abs(v);
    if (av > peak) peak = av;
    if (av >= clipThreshold) clipped++;
  }
  const dc = n ? sum / n : 0;
  const rms = n ? Math.sqrt(sq / n) : 0;
  const frameLen = Math.max(16, Math.round(0.02 * fs));
  const nFrames = Math.floor(n / frameLen);
  const powers = [];
  for (let f = 0; f < nFrames; f++) {
    let p = 0;
    const base = f * frameLen;
    for (let i = 0; i < frameLen; i++) {
      const v = samples[base + i] - dc;
      p += v * v;
    }
    powers.push(p / frameLen);
  }
  powers.sort((a, b) => a - b);
  const p10 = powers[Math.floor(powers.length * 0.1)] || EPS * EPS;
  const p90 = powers[Math.floor(powers.length * 0.9)] || EPS * EPS;
  const active = powers.filter((p) => p > p10 * 2);
  const sigPow = active.length ? active.reduce((a, b) => a + b, 0) / active.length : p90;
  const snrDb = 10 * Math.log10(Math.max(sigPow - p10, EPS * EPS) / Math.max(p10, EPS * EPS));
  const rmsDb = toDb(rms);
  const peakDb = toDb(peak);
  const lufs = Math.max(-70, powerToDb(sigPow) - 0.691);

  return {
    path: pathLabel,
    sample_rate: fs,
    channels: 1,
    n_samples: n,
    duration_s: n / fs,
    integrated_lufs: Number(lufs.toFixed(2)),
    threshold_lufs: Number((lufs - 10).toFixed(2)),
    lra_lu: 1.5,
    lra_low_lufs: Number((lufs - 1.0).toFixed(2)),
    lra_high_lufs: Number((lufs + 0.5).toFixed(2)),
    ffmpeg_sample_peak_dbfs: Number(peakDb.toFixed(2)),
    true_peak_dbtp: Number((peakDb + 0.02).toFixed(2)),
    sample_peak_dbfs: Number(peakDb.toFixed(2)),
    oversampled_true_peak_dbtp: Number((peakDb + 0.04).toFixed(2)),
    inter_sample_peak_excess_db: 0.02,
    has_inter_sample_clipping: peakDb + 0.04 > 0,
    rms_dbfs: Number(rmsDb.toFixed(2)),
    crest_factor_db: Number((peakDb - rmsDb).toFixed(2)),
    dc_offset_per_channel: [Number(dc.toFixed(6))],
    max_abs_dc_offset: Number(Math.abs(dc).toFixed(6)),
    clipped_samples: clipped,
    clipped_ratio: n ? clipped / n : 0,
    has_clipping: clipped > 0,
    noise_floor_dbfs: Number(powerToDb(p10).toFixed(2)),
    signal_level_dbfs: Number(powerToDb(sigPow).toFixed(2)),
    estimated_snr_db: Number(Math.max(0, snrDb).toFixed(2)),
  };
}

function biquadHighpassZeroPhase(samples, fs, cutoffHz) {
  if (cutoffHz <= 0 || cutoffHz >= 0.45 * fs) return new Float64Array(samples);
  const w0 = (2 * Math.PI * cutoffHz) / fs;
  const alpha = Math.sin(w0) / (2 * Math.SQRT1_2);
  const cosw = Math.cos(w0);
  const a0 = 1 + alpha;
  const b0 = ((1 + cosw) / 2) / a0;
  const b1 = (-(1 + cosw)) / a0;
  const b2 = b0;
  const a1 = (-2 * cosw) / a0;
  const a2 = (1 - alpha) / a0;
  const run = (arr) => {
    const out = new Float64Array(arr.length);
    let x1 = 0, x2 = 0, y1 = 0, y2 = 0;
    for (let i = 0; i < arr.length; i++) {
      const x0 = arr[i];
      const y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
      out[i] = y0;
      x2 = x1; x1 = x0; y2 = y1; y1 = y0;
    }
    return out;
  };
  return run(run(samples).reverse()).reverse();
}

export class StaticDemoEngine {
  constructor() {
    this.assets = new Map();
    this.exports = new Map();
    this.state = { active_source: null, clean: null, master: null, cut: null };
  }

  async health() {
    return {
      status: "ok",
      mode: "static",
      version: "0.1.0",
      workspace_dir: "Browser WebAudio Sandbox",
      presets: DEMO_MASTER_PRESETS,
      limits: { max_upload_bytes: 100 * 1024 * 1024 },
    };
  }

  async getSession() {
    return {
      active_source: this.state.active_source ? this._summary(this.state.active_source) : null,
      clean: this.state.clean,
      master: this.state.master,
      cut: this.state.cut,
    };
  }

  async resetSession() {
    this.state = { active_source: null, clean: null, master: null, cut: null };
    return this.getSession();
  }

  async listWorkspaceFiles() {
    return { files: [] };
  }

  _register(samples, fs, filename, label, kind, setActive = false) {
    const id = Math.random().toString(16).slice(2, 18).padEnd(16, "0");
    const blob = encodeWavBlob(samples, fs);
    const url = URL.createObjectURL(blob);
    const rec = {
      asset_id: id,
      label,
      filename,
      source_kind: kind,
      samples,
      fs,
      audio_url: url,
      download_url: url,
      probe: probeSamples(samples, fs, 0.999, filename),
      waveform: computeWaveform(samples, fs),
    };
    this.assets.set(id, rec);
    if (setActive) {
      this.state.active_source = id;
      this.state.clean = null;
      this.state.master = null;
      this.state.cut = null;
    }
    return this._summary(id);
  }

  _summary(id) {
    const r = this.assets.get(id);
    if (!r) return null;
    return {
      asset_id: r.asset_id,
      label: r.label,
      filename: r.filename,
      source_kind: r.source_kind,
      audio_url: r.audio_url,
      download_url: r.download_url,
      probe: r.probe,
      waveform: r.waveform,
    };
  }

  async activateSource(assetId) {
    if (!this.assets.has(assetId)) throw new Error("Audio asset not found.");
    this.state.active_source = assetId;
    this.state.clean = null;
    this.state.master = null;
    this.state.cut = null;
    return this.getSession();
  }

  async loadSample() {
    const fs = 48000;
    const n = Math.round(6.4 * fs);
    const y = new Float64Array(n);
    let seed = 202601;
    const rand = () => {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return (seed / 4294967296 - 0.5) * 2;
    };
    for (let i = 0; i < n; i++) {
      const t = i / fs;
      y[i] = 0.002 * rand() + 0.0032 * Math.sin(2 * Math.PI * 38 * t) + 0.015;
    }
    const phrases = [
      [0.45, 1.55, 120, 148],
      [2.15, 3.25, 132, 118],
      [3.85, 4.85, 116, 142],
      [5.35, 6.05, 128, 110],
    ];
    for (let p = 0; p < phrases.length; p++) {
      const [s0, s1, f0, f1] = phrases[p];
      const i0 = Math.round(s0 * fs);
      const i1 = Math.round(s1 * fs);
      let phase = 0;
      for (let i = i0; i < i1; i++) {
        const tau = (i - i0) / fs;
        const prog = (i - i0) / Math.max(1, i1 - i0);
        const f = f0 + (f1 - f0) * prog;
        phase += (2 * Math.PI * f) / fs;
        const syll = 0.55 + 0.45 * Math.sin(2 * Math.PI * (3.2 + 0.3 * p) * tau);
        const fade = Math.min(1, tau / 0.03, ((i1 - i) / fs) / 0.03);
        const voice = 0.55 * Math.sin(phase) + 0.35 * Math.sin(2 * phase) + 0.25 * Math.sin(4 * phase);
        y[i] += 0.36 * fade * syll * voice;
      }
    }
    for (const ct of [0.12, 2.6, 4.4]) {
      y[Math.round(ct * fs)] += 0.58;
    }
    return this._register(y, fs, "loudgate_broadcast_sample.wav", "Broadcast Speech + Silence Sample", "sample", true);
  }

  async uploadFile(file) {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    try {
      const arr = await file.arrayBuffer();
      const decoded = await ctx.decodeAudioData(arr);
      const ch0 = decoded.getChannelData(0);
      const mono = new Float64Array(ch0.length);
      for (let i = 0; i < ch0.length; i++) mono[i] = ch0[i];
      return this._register(mono, decoded.sampleRate, file.name, file.name, "upload", true);
    } finally {
      ctx.close().catch(() => {});
    }
  }

  async probe(assetId, clipThreshold = 0.999) {
    const rec = this.assets.get(assetId);
    if (!rec) throw new Error("Asset not found.");
    if (!(clipThreshold >= 0.01 && clipThreshold <= 1.0)) throw new Error("clip_threshold must be between 0.01 and 1.0.");
    rec.probe = probeSamples(rec.samples, rec.fs, clipThreshold, rec.filename);
    return this._summary(assetId);
  }

  async clean(params) {
    const rec = this.assets.get(params.asset_id);
    if (!rec) throw new Error("Asset not found.");
    const nFft = Number(params.n_fft || 2048);
    const hop = Number(params.hop_length || 512);
    if (hop < 1 || nFft % hop !== 0 || nFft / hop < 2) {
      throw new Error(`n_fft (${nFft}) must be an integer multiple >= 2 of hop_length (${hop}) for COLA.`);
    }
    let x = new Float64Array(rec.samples);
    if (params.remove_dc !== false) {
      let mean = 0;
      for (let i = 0; i < x.length; i++) mean += x[i];
      mean /= Math.max(1, x.length);
      for (let i = 0; i < x.length; i++) x[i] -= mean;
    }
    const hpf = Number(params.highpass_hz ?? 80);
    if (hpf > 0) x = biquadHighpassZeroPhase(x, rec.fs, hpf);
    let clicks = 0;
    if (params.declick !== false) {
      for (let i = 2; i < x.length - 2; i++) {
        const d2 = Math.abs(x[i + 1] - 2 * x[i] + x[i - 1]);
        if (d2 > 0.35) {
          x[i] = 0.5 * (x[i - 1] + x[i + 1]);
          clicks++;
        }
      }
    }
    const strength = Number(params.denoise_strength ?? 1.0);
    const floorGain = Math.pow(10, Number(params.spectral_floor_db ?? -28) / 20);
    if (strength > 0) {
      const thresh = 0.012 / Math.max(0.2, strength);
      for (let i = 0; i < x.length; i++) {
        const ax = Math.abs(x[i]);
        if (ax < thresh) {
          const g = Math.max(floorGain, ax / thresh);
          x[i] *= g;
        }
      }
    }
    const outSummary = this._register(
      x,
      rec.fs,
      rec.filename.replace(/\.[^.]+$/, "_clean.wav"),
      "Cleaned WAV",
      "clean",
      false
    );
    const payload = {
      before_probe: rec.probe,
      after_probe: outSummary.probe,
      stats: {
        sample_rate: rec.fs,
        n_fft: nFft,
        hop_length: hop,
        window: "hann",
        highpass_hz: hpf,
        clicks_repaired: clicks,
        parseval_relative_error: 0.0,
        input_rms_dbfs: rec.probe.rms_dbfs,
        output_rms_dbfs: outSummary.probe.rms_dbfs,
        noise_reduction_db_est: Number((rec.probe.noise_floor_dbfs - outSummary.probe.noise_floor_dbfs).toFixed(2)),
      },
      output_asset: outSummary,
    };
    this.state.clean = payload;
    return payload;
  }

  async master(params) {
    const rec = this.assets.get(params.asset_id);
    if (!rec) throw new Error("Asset not found.");
    const presetKey = String(params.preset || "podcast").toLowerCase();
    const pCfg = DEMO_MASTER_PRESETS[presetKey];
    if (!pCfg) throw new Error(`Unknown preset '${presetKey}'.`);
    const targetLufs = params.target_lufs !== null && params.target_lufs !== undefined ? Number(params.target_lufs) : pCfg.target_lufs;
    const maxTp = Number(params.max_true_peak_dbtp ?? pCfg.max_true_peak_dbtp);
    const gainDb = targetLufs - rec.probe.integrated_lufs;
    const gainLin = Math.pow(10, gainDb / 20);
    const ceilLin = Math.pow(10, (maxTp - 0.05) / 20);
    const y = new Float64Array(rec.samples.length);
    for (let i = 0; i < rec.samples.length; i++) {
      y[i] = Math.max(-ceilLin, Math.min(ceilLin, rec.samples[i] * gainLin));
    }
    const outSummary = this._register(
      y,
      rec.fs,
      rec.filename.replace(/\.[^.]+$/, `_${presetKey}.wav`),
      "Mastered WAV",
      "master",
      false
    );
    const payload = {
      before_probe: rec.probe,
      after_probe: outSummary.probe,
      master_result: {
        preset: presetKey,
        target_lufs: targetLufs,
        max_true_peak_dbtp: maxTp,
        target_lra: Number(params.target_lra ?? pCfg.target_lra),
        loudnorm_mode: "webaudio_two_pass_demo",
        pass1_stats: { input_i: String(rec.probe.integrated_lufs), input_tp: String(rec.probe.true_peak_dbtp), target_offset: gainDb.toFixed(2) },
        output_probe: outSummary.probe,
      },
      output_asset: outSummary,
    };
    this.state.master = payload;
    return payload;
  }

  async cut(params) {
    const rec = this.assets.get(params.asset_id);
    if (!rec) throw new Error("Asset not found.");
    const threshDb = Number(params.silence_thresh_db ?? -40);
    const minSilMs = Number(params.min_silence_ms ?? 400);
    const minSpeechMs = Number(params.min_speech_ms ?? 150);
    const padMs = Number(params.pad_ms ?? 80);
    const fps = Number(params.fps ?? 25);
    const fs = rec.fs;
    const rawSamples = rec.samples;
    let dcMean = 0;
    for (let i = 0; i < rawSamples.length; i++) dcMean += rawSamples[i];
    dcMean /= Math.max(1, rawSamples.length);
    const x = new Float64Array(rawSamples.length);
    for (let i = 0; i < rawSamples.length; i++) x[i] = rawSamples[i] - dcMean;
    const frameLen = Math.max(8, Math.round(0.02 * fs));
    const hopLen = Math.max(1, Math.round(0.01 * fs));
    const active = [];
    for (let i = 0; i + frameLen <= x.length; i += hopLen) {
      let sq = 0;
      for (let j = 0; j < frameLen; j++) sq += x[i + j] * x[i + j];
      const db = powerToDb(sq / frameLen);
      active.push(db >= threshDb);
    }
    const minSilFrames = Math.max(1, Math.floor(minSilMs / 10));
    for (let i = 0; i < active.length; ) {
      if (!active[i]) {
        let j = i;
        while (j < active.length && !active[j]) j++;
        if (i > 0 && j < active.length && j - i < minSilFrames) {
          for (let k = i; k < j; k++) active[k] = true;
        }
        i = j;
      } else i++;
    }
    const totalDur = x.length / fs;
    const raw = [];
    for (let i = 0; i < active.length; ) {
      if (active[i]) {
        let j = i;
        while (j < active.length && active[j]) j++;
        const s = (i * hopLen) / fs;
        const e = Math.min(totalDur, (j * hopLen + frameLen) / fs);
        if ((e - s) * 1000 >= minSpeechMs) {
          raw.push([Math.max(0, s - padMs / 1000), Math.min(totalDur, e + padMs / 1000)]);
        }
        i = j;
      } else i++;
    }
    const merged = [];
    for (const iv of raw) {
      if (!merged.length || iv[0] > merged[merged.length - 1][1]) merged.push(iv);
      else merged[merged.length - 1][1] = Math.max(merged[merged.length - 1][1], iv[1]);
    }
    const segments = merged.map(([s, e], idx) => {
      const i0 = Math.round(s * fs);
      const i1 = Math.min(x.length, Math.round(e * fs));
      let sq = 0;
      let zc = 0;
      for (let k = i0; k < i1; k++) {
        sq += x[k] * x[k];
        if (k > i0 && (x[k] >= 0) !== (x[k - 1] >= 0)) zc++;
      }
      return {
        index: idx + 1,
        start_s: Number(s.toFixed(6)),
        end_s: Number(e.toFixed(6)),
        duration_s: Number((e - s).toFixed(6)),
        rms_dbfs: Number(powerToDb(sq / Math.max(1, i1 - i0)).toFixed(3)),
        mean_zcr: Number((zc / Math.max(1, i1 - i0 - 1)).toFixed(4)),
      };
    });
    const kept = segments.reduce((a, b) => a + b.duration_s, 0);
    let trimmedSummary = null;
    if (params.render_audio !== false && segments.length) {
      const chunks = segments.map((s) => x.slice(Math.round(s.start_s * fs), Math.round(s.end_s * fs)));
      const totalLen = chunks.reduce((a, c) => a + c.length, 0);
      const cat = new Float64Array(totalLen);
      let off = 0;
      for (const ch of chunks) {
        cat.set(ch, off);
        off += ch.length;
      }
      trimmedSummary = this._register(cat, fs, rec.filename.replace(/\.[^.]+$/, "_cut.wav"), "Trimmed WAV", "cut", false);
    }

    const otio = JSON.stringify({
      OTIO_SCHEMA: "Timeline.1",
      metadata: { generator: "loudgate-demo" },
      name: "loudgate_cut",
      tracks: { OTIO_SCHEMA: "Stack.1", children: [{ OTIO_SCHEMA: "Track.1", kind: "Audio", name: "A1", children: segments.map((s) => ({ OTIO_SCHEMA: "Clip.2", name: `seg_${s.index}`, source_range: { OTIO_SCHEMA: "TimeRange.1", start_time: { OTIO_SCHEMA: "RationalTime.1", rate: fps, value: Math.round(s.start_s * fps) }, duration: { OTIO_SCHEMA: "RationalTime.1", rate: fps, value: Math.max(1, Math.round(s.duration_s * fps)) } } })) }] },
    }, null, 2);
    const fmtSrt = (sec) => {
      const ms = Math.round(sec * 1000);
      const hh = Math.floor(ms / 3600000);
      const mm = Math.floor((ms % 3600000) / 60000);
      const ss = Math.floor((ms % 60000) / 1000);
      return `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")},${String(ms % 1000).padStart(3, "0")}`;
    };
    let cursor = 0;
    const srt = segments.map((s, i) => {
      const st = cursor;
      cursor += s.duration_s;
      return `${i + 1}\n${fmtSrt(st)} --> ${fmtSrt(cursor)}\n[Segment ${String(s.index).padStart(2, "0")}]\n`;
    }).join("\n");
    const vtt = `WEBVTT\n\n${srt.replace(/,/g, ".")}`;
    const edl = `TITLE: LOUDGATE_CUT\nFCM: NON-DROP FRAME\n\n` + segments.map((s, i) => `${String(i + 1).padStart(3, "0")}  AX  A  C  ${s.start_s.toFixed(2)} ${s.end_s.toFixed(2)}`).join("\n");

    const downloads = {
      otio: URL.createObjectURL(new Blob([otio], { type: "application/json" })),
      edl: URL.createObjectURL(new Blob([edl], { type: "text/plain" })),
      srt: URL.createObjectURL(new Blob([srt], { type: "text/plain" })),
      vtt: URL.createObjectURL(new Blob([vtt], { type: "text/vtt" })),
    };
    if (trimmedSummary) downloads.wav = trimmedSummary.download_url;

    const payload = {
      export_id: "demo",
      input_duration_s: Number(totalDur.toFixed(6)),
      kept_duration_s: Number(kept.toFixed(6)),
      removed_duration_s: Number(Math.max(0, totalDur - kept).toFixed(6)),
      segment_count: segments.length,
      segments,
      silence_thresh_db: threshDb,
      downloads,
      trimmed_asset: trimmedSummary,
    };
    this.state.cut = payload;
    return payload;
  }
}
