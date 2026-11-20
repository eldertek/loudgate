import { StudioEngine } from "./engine.js";
import { t } from "./i18n.js";
import { WaveformRenderer } from "./waveform.js";

const STORAGE_LANG_KEY = "loudgate_ui_lang";
const STORAGE_TAB_KEY = "loudgate_ui_tab";
const LED_SEGMENTS = 26;

const engine = new StudioEngine();

const state = {
  lang: localStorage.getItem(STORAGE_LANG_KEY) === "fr" ? "fr" : "en",
  activeTab: localStorage.getItem(STORAGE_TAB_KEY) || "probe",
  presets: {},
  selectedPreset: "podcast",
  activeSource: null,
  cleanResult: null,
  masterResult: null,
  cutResult: null,
  auditionSlot: "A",
  looping: false,
  fps: 25,
};

const el = (id) => document.getElementById(id);

function createNode(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function fmtNum(val, digits = 2, suffix = "") {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "--";
  return `${Number(val).toFixed(digits)}${suffix}`;
}

function fmtSmpte(seconds, fps = 25) {
  const s = Math.max(0, Number(seconds) || 0);
  const totalFrames = Math.floor(s * fps);
  const fpsInt = Math.max(1, Math.round(fps));
  const ff = totalFrames % fpsInt;
  const totalSec = Math.floor(totalFrames / fpsInt);
  const ss = totalSec % 60;
  const mm = Math.floor(totalSec / 60) % 60;
  const hh = Math.floor(totalSec / 3600);
  return `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}:${String(ff).padStart(2, "0")}`;
}

function fmtTimeMs(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  return `${s.toFixed(3).padStart(6, "0")}s`;
}

function showAlert(title, message) {
  el("alert-title").textContent = title || t(state.lang, "error.validationTitle");
  el("alert-message").textContent = message;
  el("alert-banner").classList.remove("hidden");
}

function clearAlert() {
  el("alert-banner").classList.add("hidden");
  el("alert-message").textContent = "";
}

function setBusy(busy, messageKey = "") {
  const toast = el("status-toast");
  if (busy) {
    el("status-toast-text").textContent = t(state.lang, messageKey) || "Processing...";
    toast.classList.remove("hidden");
  } else {
    toast.classList.add("hidden");
  }
  const ids = [
    "btn-load-sample",
    "btn-load-workspace",
    "btn-run-probe",
    "btn-run-clean",
    "btn-run-master",
    "btn-run-cut",
  ];
  for (const id of ids) {
    const btn = el(id);
    if (btn) {
      btn.disabled = busy || (!state.activeSource && id.startsWith("btn-run-"));
    }
  }
}

// Live WebAudio Meter Bridge state
const meterAudio = {
  ctx: null,
  analyser: null,
  sourceNode: null,
  buf: null,
  rafId: 0,
  shortTermBuf: [],
};

function buildLedColumns() {
  for (const colId of ["led-col-m", "led-col-s", "led-col-i", "led-col-tp"]) {
    const col = el(colId);
    const segs = [];
    for (let i = 0; i < LED_SEGMENTS; i++) {
      const seg = createNode("div", "led-seg");
      const ratio = i / (LED_SEGMENTS - 1);
      if (ratio >= 0.91) seg.dataset.zone = "red";
      else if (ratio >= 0.72) seg.dataset.zone = "amber";
      else seg.dataset.zone = "green";
      segs.push(seg);
    }
    col.replaceChildren(...segs);
  }
}

function paintLedColumn(colId, numId, dbVal, minDb, maxDb, suffix = "") {
  const col = el(colId);
  const num = el(numId);
  if (dbVal === null || dbVal === undefined || Number.isNaN(Number(dbVal))) {
    col.querySelectorAll(".led-seg").forEach((s) => s.classList.remove("on", "green", "amber", "red"));
    num.textContent = "--";
    return;
  }
  const val = Number(dbVal);
  const ratio = Math.max(0, Math.min(1, (val - minDb) / (maxDb - minDb)));
  const litCount = Math.round(ratio * LED_SEGMENTS);
  const segs = col.children;
  for (let i = 0; i < segs.length; i++) {
    const s = segs[i];
    const isOn = i < litCount;
    s.classList.toggle("on", isOn);
    s.classList.remove("green", "amber", "red");
    if (isOn) s.classList.add(s.dataset.zone);
  }
  num.textContent = `${val.toFixed(1)}${suffix}`;
}

function ensureAnalyserConnected() {
  if (meterAudio.analyser) return;
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) return;
  try {
    const player = el("ab-audio-player");
    meterAudio.ctx = new AudioCtx();
    meterAudio.analyser = meterAudio.ctx.createAnalyser();
    meterAudio.analyser.fftSize = 1024;
    meterAudio.buf = new Float32Array(meterAudio.analyser.fftSize);
    meterAudio.sourceNode = meterAudio.ctx.createMediaElementSource(player);
    meterAudio.sourceNode.connect(meterAudio.analyser);
    meterAudio.analyser.connect(meterAudio.ctx.destination);
  } catch {
    // Graceful fallback if browser blocks analyser media hook
  }
}

function startLiveMeters() {
  ensureAnalyserConnected();
  if (meterAudio.ctx && meterAudio.ctx.state === "suspended") {
    meterAudio.ctx.resume().catch(() => {});
  }
  if (meterAudio.rafId) return;
  const tick = () => {
    const player = el("ab-audio-player");
    updateTransportReadout();
    if (meterAudio.analyser && meterAudio.buf && !player.paused) {
      meterAudio.analyser.getFloatTimeDomainData(meterAudio.buf);
      let sq = 0;
      let pk = 0;
      for (let i = 0; i < meterAudio.buf.length; i++) {
        const v = meterAudio.buf[i];
        sq += v * v;
        const av = Math.abs(v);
        if (av > pk) pk = av;
      }
      const rmsDb = Math.max(-60, 10 * Math.log10(Math.max(1e-12, sq / meterAudio.buf.length)) - 0.7);
      const pkDb = Math.max(-60, 20 * Math.log10(Math.max(1e-12, pk)));
      meterAudio.shortTermBuf.push(rmsDb);
      if (meterAudio.shortTermBuf.length > 90) meterAudio.shortTermBuf.shift();
      const shortDb = meterAudio.shortTermBuf.reduce((a, b) => a + b, 0) / meterAudio.shortTermBuf.length;

      paintLedColumn("led-col-m", "led-num-m", rmsDb, -60, 3);
      paintLedColumn("led-col-s", "led-num-s", shortDb, -60, 3);
      paintLedColumn("led-col-tp", "led-num-tp", pkDb, -60, 3);
    }
    if (!player.paused) {
      meterAudio.rafId = requestAnimationFrame(tick);
    } else {
      meterAudio.rafId = 0;
    }
  };
  meterAudio.rafId = requestAnimationFrame(tick);
}

const waveform = new WaveformRenderer(
  el("waveform-canvas"),
  (seekTimeS) => {
    const player = el("ab-audio-player");
    if (!player || !player.getAttribute("src")) return;
    const dur = Number.isFinite(player.duration) && player.duration > 0
      ? player.duration
      : (state.activeSource ? state.activeSource.probe.duration_s : 0);
    if (dur > 0) {
      player.currentTime = Math.max(0, Math.min(dur, seekTimeS));
      updateTransportReadout();
    }
  },
  (hoverTimeS, freqHz) => {
    const freqTxt = freqHz !== null ? `${freqHz >= 1000 ? `${(freqHz / 1000).toFixed(2)} kHz` : `${Math.round(freqHz)} Hz`}` : "-- Hz";
    el("waveform-cursor-info").textContent = `${fmtSmpte(hoverTimeS, state.fps)} | ${freqTxt}`;
  },
  (newThreshDb) => {
    el("cut-thresh-range").value = String(newThreshDb);
    el("cut-thresh-db").value = String(newThreshDb);
    el("cut-thresh-readout").textContent = `${newThreshDb} dB`;
  }
);

function applyLanguage(lang) {
  state.lang = lang === "fr" ? "fr" : "en";
  localStorage.setItem(STORAGE_LANG_KEY, state.lang);
  document.documentElement.lang = state.lang;

  el("lang-en").classList.toggle("active", state.lang === "en");
  el("lang-en").setAttribute("aria-pressed", String(state.lang === "en"));
  el("lang-fr").classList.toggle("active", state.lang === "fr");
  el("lang-fr").setAttribute("aria-pressed", String(state.lang === "fr"));

  document.querySelectorAll("[data-i18n]").forEach((node) => {
    const key = node.getAttribute("data-i18n");
    if (key) node.textContent = t(state.lang, key);
  });

  renderAll();
}

function getProcessedAssetForTab() {
  if (state.activeTab === "clean" && state.cleanResult) {
    return { asset: state.cleanResult.output_asset, labelKey: "transport.auditionCleaned" };
  }
  if (state.activeTab === "master" && state.masterResult) {
    return { asset: state.masterResult.output_asset, labelKey: "transport.auditionMastered" };
  }
  if (state.activeTab === "cut" && state.cutResult && state.cutResult.trimmed_asset) {
    return { asset: state.cutResult.trimmed_asset, labelKey: "transport.auditionCut" };
  }
  return null;
}

function getPreferredBAsset() {
  const tabMatch = getProcessedAssetForTab();
  if (tabMatch) return tabMatch;
  if (state.masterResult) return { asset: state.masterResult.output_asset, labelKey: "transport.auditionMastered" };
  if (state.cleanResult) return { asset: state.cleanResult.output_asset, labelKey: "transport.auditionCleaned" };
  if (state.cutResult && state.cutResult.trimmed_asset) {
    return { asset: state.cutResult.trimmed_asset, labelKey: "transport.auditionCut" };
  }
  return null;
}

function setAuditionSlot(slot, autoplay = false) {
  const player = el("ab-audio-player");
  const prevTime = player.currentTime || 0;
  const wasPlaying = !player.paused;

  const bInfo = getPreferredBAsset();
  state.auditionSlot = slot === "B" && bInfo ? "B" : "A";

  const targetAsset = state.auditionSlot === "B" && bInfo ? bInfo.asset : state.activeSource;
  el("btn-audition-a").classList.toggle("active", state.auditionSlot === "A");
  el("btn-audition-b").classList.toggle("active", state.auditionSlot === "B");
  el("btn-play-a").classList.toggle("active", state.auditionSlot === "A");
  el("btn-play-b").classList.toggle("active", state.auditionSlot === "B");

  if (!targetAsset) {
    player.removeAttribute("src");
    player.load();
    updateTransportReadout();
    return;
  }

  const nextUrl = targetAsset.audio_url;
  if (player.getAttribute("src") !== nextUrl) {
    player.setAttribute("src", nextUrl);
    player.load();
    player.addEventListener(
      "loadedmetadata",
      () => {
        if (prevTime > 0 && Number.isFinite(player.duration) && prevTime < player.duration) {
          player.currentTime = prevTime;
        }
        if (autoplay || wasPlaying) {
          player.play().catch(() => {});
        }
        updateTransportReadout();
      },
      { once: true }
    );
  } else if (autoplay) {
    player.play().catch(() => {});
  }
  renderWaveform();
  renderMeterBridge();
}

function updateTransportReadout() {
  const player = el("ab-audio-player");
  const cur = player.currentTime || 0;
  el("transport-time-readout").textContent = `${fmtSmpte(cur, state.fps)} | ${fmtTimeMs(cur)}`;
  waveform.setPlayhead(cur);
}

function renderSourceDock() {
  const metaWrap = el("active-source-meta");
  const hasSource = Boolean(state.activeSource);

  el("btn-run-probe").disabled = !hasSource;
  el("btn-download-probe-json").disabled = !hasSource;
  el("btn-run-clean").disabled = !hasSource;
  el("btn-run-master").disabled = !hasSource;
  el("btn-run-cut").disabled = !hasSource;
  el("btn-audition-a").disabled = !hasSource;
  el("btn-play-a").disabled = !hasSource;
  el("btn-rewind").disabled = !hasSource;
  el("btn-stop").disabled = !hasSource;
  el("btn-loop").disabled = !hasSource;

  const bInfo = getPreferredBAsset();
  el("btn-audition-b").disabled = !bInfo;
  el("btn-play-b").disabled = !bInfo;
  el("audition-b-label").textContent = t(state.lang, bInfo ? bInfo.labelKey : "transport.auditionB");

  if (!hasSource) {
    const hint = createNode("span", "empty-source-hint", t(state.lang, "source.emptyHint"));
    hint.id = "empty-source-hint";
    metaWrap.replaceChildren(hint);
    return;
  }

  const s = state.activeSource;
  const p = s.probe;
  metaWrap.replaceChildren(
    createNode("span", "daw-tag", s.source_kind.toUpperCase()),
    createNode("span", "source-chip", s.filename),
    createNode("span", "source-chip", `${(p.sample_rate / 1000).toFixed(1)} kHz | ${p.channels}ch | ${p.duration_s.toFixed(2)}s`),
    createNode("span", "source-chip", `${p.integrated_lufs.toFixed(1)} LUFS | ${p.true_peak_dbtp.toFixed(1)} dBTP`)
  );
}

function getInspectedAsset() {
  if (state.auditionSlot === "B") {
    const bInfo = getPreferredBAsset();
    if (bInfo && bInfo.asset) return bInfo.asset;
  }
  return state.activeSource;
}

function renderMeterBridge() {
  const asset = getInspectedAsset();
  const clipBadge = el("clip-led-badge");

  if (!asset) {
    paintLedColumn("led-col-m", "led-num-m", null, -60, 3);
    paintLedColumn("led-col-s", "led-num-s", null, -60, 3);
    paintLedColumn("led-col-i", "led-num-i", null, -60, 3);
    paintLedColumn("led-col-tp", "led-num-tp", null, -60, 3);
    el("meter-lufs-val").textContent = "-- LUFS";
    el("bridge-lra-val").textContent = "-- LU";
    el("meter-tp-val").textContent = "-- dBTP";
    el("bridge-ostp-val").textContent = "-- dBTP";
    el("meter-snr-val").textContent = "-- dB";
    el("bridge-dc-val").textContent = "--";
    clipBadge.className = "clip-led";
    clipBadge.textContent = "CLIP OK";
    return;
  }

  const p = asset.probe;
  const player = el("ab-audio-player");
  if (player.paused) {
    paintLedColumn("led-col-m", "led-num-m", p.signal_level_dbfs, -60, 3);
    paintLedColumn("led-col-s", "led-num-s", p.integrated_lufs, -60, 3);
    paintLedColumn("led-col-tp", "led-num-tp", p.oversampled_true_peak_dbtp, -60, 3);
  }
  paintLedColumn("led-col-i", "led-num-i", p.integrated_lufs, -60, 3);

  el("meter-lufs-val").textContent = `${p.integrated_lufs.toFixed(2)} LUFS`;
  el("bridge-lra-val").textContent = `${p.lra_lu.toFixed(2)} LU`;
  el("meter-tp-val").textContent = `${p.true_peak_dbtp.toFixed(2)} dBTP`;
  el("bridge-ostp-val").textContent = `${p.oversampled_true_peak_dbtp.toFixed(2)} dBTP`;
  el("meter-snr-val").textContent = `${p.estimated_snr_db.toFixed(1)} dB`;
  el("bridge-dc-val").textContent = `${p.max_abs_dc_offset.toFixed(5)}`;

  const maxTp = Math.max(p.true_peak_dbtp, p.oversampled_true_peak_dbtp);
  if (p.has_clipping || p.has_inter_sample_clipping || maxTp > 0.0) {
    clipBadge.className = "clip-led clip";
    clipBadge.textContent = "CLIP OVER";
  } else if (maxTp > -1.0) {
    clipBadge.className = "clip-led hot";
    clipBadge.textContent = "HOT > -1";
  } else {
    clipBadge.className = "clip-led";
    clipBadge.textContent = "CLIP OK";
  }
}

function renderWaveform() {
  const legendThresh = el("legend-thresh");
  const legendKept = el("legend-kept");
  const legendCut = el("legend-cut");
  const trackBadge = el("waveform-track-badge");

  if (!state.activeSource) {
    legendThresh.classList.add("hidden");
    legendKept.classList.add("hidden");
    legendCut.classList.add("hidden");
    trackBadge.textContent = "--";
    waveform.setData({ waveform: null, emptyMessage: t(state.lang, "waveform.emptyCanvas") });
    return;
  }

  let displayWf = state.activeSource.waveform;
  let cutOverlay = null;
  let badgeKey = "waveform.trackSource";

  legendThresh.classList.toggle("hidden", state.activeTab !== "cut");
  if (state.activeTab === "cut" && state.cutResult) {
    cutOverlay = state.cutResult;
    badgeKey = "waveform.trackCut";
    legendKept.classList.remove("hidden");
    legendCut.classList.remove("hidden");
  } else {
    legendKept.classList.add("hidden");
    legendCut.classList.add("hidden");
    if (state.auditionSlot === "B") {
      const bInfo = getPreferredBAsset();
      if (bInfo && bInfo.asset && bInfo.asset.waveform) {
        displayWf = bInfo.asset.waveform;
        badgeKey = bInfo.asset.source_kind === "clean" ? "waveform.trackCleaned" : "waveform.trackMastered";
      }
    }
  }

  trackBadge.textContent = t(state.lang, badgeKey);
  waveform.setData({
    waveform: displayWf,
    cutData: cutOverlay,
    threshDb: Number(el("cut-thresh-db").value),
    showThreshold: state.activeTab === "cut",
    playheadRatio: 0,
    emptyMessage: t(state.lang, "waveform.emptyCanvas"),
  });
}

function renderProbeTab() {
  const grid = el("probe-metrics-grid");
  if (!state.activeSource) {
    grid.replaceChildren();
    return;
  }
  const p = state.activeSource.probe;
  const tiles = [
    ["Integrated LUFS", fmtNum(p.integrated_lufs, 2, " LUFS"), `Gate ${fmtNum(p.threshold_lufs, 2, " LUFS")}`],
    ["Loudness Range", fmtNum(p.lra_lu, 2, " LU"), `${fmtNum(p.lra_low_lufs, 1)} .. ${fmtNum(p.lra_high_lufs, 1)}`],
    ["FFmpeg True Peak", fmtNum(p.true_peak_dbtp, 2, " dBTP"), `Sample ${fmtNum(p.ffmpeg_sample_peak_dbfs, 2, " dBFS")}`],
    ["4x Oversampled TP", fmtNum(p.oversampled_true_peak_dbtp, 2, " dBTP"), `ISP +${fmtNum(p.inter_sample_peak_excess_db, 2, " dB")}`],
    ["Sample Peak / RMS", fmtNum(p.sample_peak_dbfs, 2, " dBFS"), `RMS ${fmtNum(p.rms_dbfs, 2, " dBFS")}`],
    ["Crest / SNR", `${fmtNum(p.crest_factor_db, 2, " dB")}`, `SNR ${fmtNum(p.estimated_snr_db, 1, " dB")}`],
    ["Noise Floor / Sig", fmtNum(p.noise_floor_dbfs, 1, " dBFS"), `Sig ${fmtNum(p.signal_level_dbfs, 1, " dBFS")}`],
    ["Max DC Offset", Number(p.max_abs_dc_offset).toFixed(6), `Ch ${p.dc_offset_per_channel.map((v) => v.toFixed(4)).join(",")}`],
    ["Clipped Samples", `${p.clipped_samples}`, `${(p.clipped_ratio * 100).toFixed(4)}%`],
    ["Stream Spec", `${p.sample_rate} Hz / ${p.channels}ch`, `${p.duration_s.toFixed(3)}s`],
  ];
  grid.replaceChildren(
    ...tiles.map(([label, val, sub]) => {
      const card = createNode("div", "metric-tile");
      card.append(
        createNode("div", "metric-tile-label", label),
        createNode("div", "metric-tile-value", val),
        createNode("div", "metric-tile-sub", sub)
      );
      return card;
    })
  );
}

function makeDiffCard(label, beforeVal, afterVal, deltaText, deltaClass = "") {
  const card = createNode("div", "diff-card");
  const values = createNode("div", "diff-values");
  values.append(createNode("span", "diff-before", beforeVal), createNode("span", "diff-after", afterVal));
  card.append(createNode("div", "diff-title", label), values, createNode("span", `diff-delta ${deltaClass}`.trim(), deltaText));
  return card;
}

function renderCleanTab() {
  const empty = el("clean-results-empty");
  const body = el("clean-results-body");
  const btnPlayA = el("btn-clean-play-a");
  const btnPlayB = el("btn-clean-play-b");
  const dlLink = el("link-download-cleaned");
  const btnUse = el("btn-use-cleaned");

  if (!state.cleanResult) {
    empty.classList.remove("hidden");
    body.classList.add("hidden");
    btnPlayA.disabled = !state.activeSource;
    btnPlayB.disabled = true;
    btnUse.disabled = true;
    dlLink.classList.add("disabled-link");
    dlLink.setAttribute("aria-disabled", "true");
    dlLink.setAttribute("href", "#");
    return;
  }

  empty.classList.add("hidden");
  body.classList.remove("hidden");
  btnPlayA.disabled = false;
  btnPlayB.disabled = false;
  btnUse.disabled = false;

  const outAsset = state.cleanResult.output_asset;
  dlLink.classList.remove("disabled-link");
  dlLink.setAttribute("aria-disabled", "false");
  dlLink.setAttribute("href", outAsset.download_url);
  dlLink.setAttribute("download", outAsset.filename);

  const b = state.cleanResult.before_probe;
  const a = state.cleanResult.after_probe;
  const stats = state.cleanResult.stats;
  const snrDelta = a.estimated_snr_db - b.estimated_snr_db;
  const floorDelta = a.noise_floor_dbfs - b.noise_floor_dbfs;
  const lufsDelta = a.integrated_lufs - b.integrated_lufs;
  const dcDelta = a.max_abs_dc_offset - b.max_abs_dc_offset;
  const tpDelta = a.true_peak_dbtp - b.true_peak_dbtp;

  el("clean-diff-grid").replaceChildren(
    makeDiffCard("SNR", fmtNum(b.estimated_snr_db, 1, " dB"), fmtNum(a.estimated_snr_db, 1, " dB"), `${snrDelta >= 0 ? "+" : ""}${snrDelta.toFixed(1)} dB`, snrDelta > 0.5 ? "good" : ""),
    makeDiffCard("Floor", fmtNum(b.noise_floor_dbfs, 1, " dBFS"), fmtNum(a.noise_floor_dbfs, 1, " dBFS"), `${floorDelta >= 0 ? "+" : ""}${floorDelta.toFixed(1)} dB`, floorDelta < -0.5 ? "good" : ""),
    makeDiffCard("LUFS", fmtNum(b.integrated_lufs, 2), fmtNum(a.integrated_lufs, 2), `${lufsDelta >= 0 ? "+" : ""}${lufsDelta.toFixed(2)} LU`),
    makeDiffCard("DC", b.max_abs_dc_offset.toFixed(5), a.max_abs_dc_offset.toFixed(5), `${dcDelta >= 0 ? "+" : ""}${dcDelta.toFixed(5)}`, a.max_abs_dc_offset < 1e-4 ? "good" : ""),
    makeDiffCard("True Peak", fmtNum(b.true_peak_dbtp, 2), fmtNum(a.true_peak_dbtp, 2), `${tpDelta >= 0 ? "+" : ""}${tpDelta.toFixed(2)} dB`)
  );

  const statItems = [
    ["Clicks", String(stats.clicks_repaired)],
    ["Parseval Err", Number(stats.parseval_relative_error).toExponential(2)],
    ["NR Est", `${Number(stats.noise_reduction_db_est).toFixed(2)} dB`],
    ["STFT", `${stats.n_fft}/${stats.hop_length}`],
  ];
  el("clean-dsp-stats").replaceChildren(
    ...statItems.map(([k, v]) => {
      const chip = createNode("span", "stat-chip", `${k}: `);
      chip.append(createNode("strong", "", v));
      return chip;
    })
  );
}

function selectPreset(presetName) {
  const cfg = state.presets[presetName];
  if (!cfg) return;
  state.selectedPreset = presetName;
  el("master-target-lufs").value = String(cfg.target_lufs);
  el("master-max-tp").value = String(cfg.max_true_peak_dbtp);
  el("master-target-lra").value = String(cfg.target_lra);
  el("ribbon-preset-select").value = presetName;
  renderPresetPills();
}

function renderPresetPills() {
  const rack = el("master-presets-rack");
  const rib = el("ribbon-preset-select");
  const entries = Object.entries(state.presets || {}).sort(([a], [b]) => a.localeCompare(b));

  rack.replaceChildren(
    ...entries.map(([name, cfg]) => {
      const btn = createNode("button", "preset-pill", `${name} ${cfg.target_lufs}`);
      btn.type = "button";
      btn.dataset.preset = name;
      if (name === state.selectedPreset) btn.classList.add("active");
      btn.addEventListener("click", () => selectPreset(name));
      return btn;
    })
  );

  rib.replaceChildren(
    ...entries.map(([name, cfg]) => {
      const opt = createNode("option", "", `${name} (${cfg.target_lufs} LUFS)`);
      opt.value = name;
      return opt;
    })
  );
  rib.value = state.selectedPreset;
}

function renderMasterTab() {
  const empty = el("master-results-empty");
  const body = el("master-results-body");
  const btnPlayA = el("btn-master-play-a");
  const btnPlayB = el("btn-master-play-b");
  const dlLink = el("link-download-mastered");
  const btnUse = el("btn-use-mastered");

  if (!state.masterResult) {
    empty.classList.remove("hidden");
    body.classList.add("hidden");
    btnPlayA.disabled = !state.activeSource;
    btnPlayB.disabled = true;
    btnUse.disabled = true;
    dlLink.classList.add("disabled-link");
    dlLink.setAttribute("aria-disabled", "true");
    dlLink.setAttribute("href", "#");
    return;
  }

  empty.classList.add("hidden");
  body.classList.remove("hidden");
  btnPlayA.disabled = false;
  btnPlayB.disabled = false;
  btnUse.disabled = false;

  const outAsset = state.masterResult.output_asset;
  dlLink.classList.remove("disabled-link");
  dlLink.setAttribute("aria-disabled", "false");
  dlLink.setAttribute("href", outAsset.download_url);
  dlLink.setAttribute("download", outAsset.filename);

  const mr = state.masterResult.master_result;
  const b = state.masterResult.before_probe;
  const a = state.masterResult.after_probe;
  const tpOk = a.true_peak_dbtp <= mr.max_true_peak_dbtp + 0.05;

  el("master-compliance-bar").replaceChildren(
    createNode(
      "span",
      `badge ${tpOk ? "badge-green" : "badge-amber"}`,
      tpOk
        ? `COMPLIANT: ${a.integrated_lufs.toFixed(2)} LUFS | TP ${a.true_peak_dbtp.toFixed(2)} <= ${mr.max_true_peak_dbtp.toFixed(1)} dBTP`
        : `CHECK CEILING: TP ${a.true_peak_dbtp.toFixed(2)} dBTP`
    ),
    createNode("span", "stat-chip", `${mr.loudnorm_mode} | ${mr.preset}`)
  );

  const lufsErr = a.integrated_lufs - mr.target_lufs;
  el("master-diff-grid").replaceChildren(
    makeDiffCard("LUFS", fmtNum(b.integrated_lufs, 2), fmtNum(a.integrated_lufs, 2), `Target ${mr.target_lufs.toFixed(1)} (${lufsErr >= 0 ? "+" : ""}${lufsErr.toFixed(2)})`, Math.abs(lufsErr) <= 1.0 ? "good" : "warn"),
    makeDiffCard("True Peak", fmtNum(b.true_peak_dbtp, 2), fmtNum(a.true_peak_dbtp, 2), `<= ${mr.max_true_peak_dbtp.toFixed(1)} dBTP`, tpOk ? "good" : "warn"),
    makeDiffCard("4x OS TP", fmtNum(b.oversampled_true_peak_dbtp, 2), fmtNum(a.oversampled_true_peak_dbtp, 2), `ISP +${a.inter_sample_peak_excess_db.toFixed(2)} dB`, a.oversampled_true_peak_dbtp <= mr.max_true_peak_dbtp + 0.1 ? "good" : "warn"),
    makeDiffCard("LRA", fmtNum(b.lra_lu, 2), fmtNum(a.lra_lu, 2), `Target ${mr.target_lra.toFixed(1)} LU`)
  );

  const pass1 = mr.pass1_stats || {};
  el("master-pass-stats").replaceChildren(
    ...[["P1 Input I", `${pass1.input_i || "--"} LUFS`], ["P1 Input TP", `${pass1.input_tp || "--"} dBTP`], ["Offset", `${pass1.target_offset || "--"} dB`], ["File", outAsset.filename]].map(([k, v]) => {
      const chip = createNode("span", "stat-chip", `${k}: `);
      chip.append(createNode("strong", "", v));
      return chip;
    })
  );
}

function setExportLink(id, url, filename) {
  const node = el(id);
  if (!node) return;
  if (!url) {
    node.classList.add("disabled-link");
    node.setAttribute("aria-disabled", "true");
    node.setAttribute("href", "#");
  } else {
    node.classList.remove("disabled-link");
    node.setAttribute("aria-disabled", "false");
    node.setAttribute("href", url);
    if (filename) node.setAttribute("download", filename);
  }
}

function renderCutTab() {
  const empty = el("cut-results-empty");
  const body = el("cut-results-body");
  const btnPlayA = el("btn-cut-play-a");
  const btnPlayB = el("btn-cut-play-b");

  if (!state.cutResult) {
    empty.classList.remove("hidden");
    body.classList.add("hidden");
    btnPlayA.disabled = !state.activeSource;
    btnPlayB.disabled = true;
    for (const k of ["otio", "edl", "srt", "vtt", "wav"]) setExportLink(`sb-export-${k}`, null);
    return;
  }

  empty.classList.add("hidden");
  body.classList.remove("hidden");
  btnPlayA.disabled = false;
  btnPlayB.disabled = !state.cutResult.trimmed_asset;

  const c = state.cutResult;
  const savedPct = c.input_duration_s > 0 ? (c.removed_duration_s / c.input_duration_s) * 100 : 0;
  el("cut-kpi-strip").replaceChildren(
    ...[
      ["Segments", String(c.segment_count), "VAD speech"],
      ["Input", `${c.input_duration_s.toFixed(2)}s`, "Source"],
      ["Kept", `${c.kept_duration_s.toFixed(2)}s`, `${(100 - savedPct).toFixed(1)}%`],
      ["Trimmed", `${c.removed_duration_s.toFixed(2)}s`, `-${savedPct.toFixed(1)}%`],
    ].map(([lbl, val, sub]) => {
      const tile = createNode("div", "metric-tile");
      tile.append(createNode("div", "metric-tile-label", lbl), createNode("div", "metric-tile-value", val), createNode("div", "metric-tile-sub", sub));
      return tile;
    })
  );

  const dl = c.downloads || {};
  for (const kind of ["otio", "edl", "srt", "vtt", "wav"]) {
    setExportLink(`export-link-${kind}`, dl[kind], `loudgate_export.${kind}`);
    setExportLink(`sb-export-${kind}`, dl[kind], `loudgate_export.${kind}`);
  }

  const tbody = el("cut-segments-tbody");
  if (!c.segments || c.segments.length === 0) {
    const tr = createNode("tr");
    const td = createNode("td", "", "0 active segments detected above threshold. Lower Threshold dBFS.");
    td.colSpan = 6;
    tr.append(td);
    tbody.replaceChildren(tr);
    return;
  }

  tbody.replaceChildren(
    ...c.segments.map((seg) => {
      const tr = createNode("tr");
      tr.title = `Seek to ${seg.start_s.toFixed(2)}s`;
      tr.append(
        createNode("td", "", `#${seg.index}`),
        createNode("td", "", fmtSmpte(seg.start_s, state.fps)),
        createNode("td", "", fmtSmpte(seg.end_s, state.fps)),
        createNode("td", "", `${seg.duration_s.toFixed(3)}s`),
        createNode("td", "", `${seg.rms_dbfs.toFixed(1)}`),
        createNode("td", "", `${seg.mean_zcr.toFixed(3)}`)
      );
      tr.addEventListener("click", () => {
        setAuditionSlot("A", false);
        el("ab-audio-player").currentTime = seg.start_s;
        updateTransportReadout();
      });
      return tr;
    })
  );
}

function renderStatusBar() {
  const fsEl = el("status-fs-ch");
  const snrEl = el("status-snr-delta");
  if (!state.activeSource) {
    fsEl.textContent = "-- Hz | -- ch | --s";
    snrEl.textContent = "SNR: --";
    return;
  }
  const p = state.activeSource.probe;
  fsEl.textContent = `${p.sample_rate} Hz | ${p.channels}ch | ${p.duration_s.toFixed(2)}s`;
  if (state.cleanResult) {
    const bSnr = state.cleanResult.before_probe.estimated_snr_db;
    const aSnr = state.cleanResult.after_probe.estimated_snr_db;
    snrEl.textContent = `SNR: ${bSnr.toFixed(1)} -> ${aSnr.toFixed(1)} dB (+${(aSnr - bSnr).toFixed(1)} dB)`;
  } else {
    snrEl.textContent = `SNR: ${p.estimated_snr_db.toFixed(1)} dB | LUFS: ${p.integrated_lufs.toFixed(1)}`;
  }
}

function renderAll() {
  renderSourceDock();
  renderMeterBridge();
  renderProbeTab();
  renderCleanTab();
  renderMasterTab();
  renderCutTab();
  renderWaveform();
  renderStatusBar();
}

function switchTab(tabName) {
  const allowed = new Set(["probe", "clean", "master", "cut"]);
  const nextTab = allowed.has(tabName) ? tabName : "probe";
  state.activeTab = nextTab;
  localStorage.setItem(STORAGE_TAB_KEY, nextTab);

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    const active = btn.dataset.tab === nextTab;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", String(active));
  });

  document.querySelectorAll(".tab-panel").forEach((panel) => {
    const active = panel.id === `panel-${nextTab}`;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });

  renderSourceDock();
  renderWaveform();
  renderMeterBridge();
}

function syncRangeAndNumber(rangeId, numId, readoutId, unit, decimals = 0, onInputExtra = null) {
  const range = el(rangeId);
  const num = el(numId);
  const readout = el(readoutId);
  const update = (val) => {
    readout.textContent = unit ? `${Number(val).toFixed(decimals)} ${unit}` : Number(val).toFixed(decimals);
    if (onInputExtra) onInputExtra(Number(val));
  };
  range.addEventListener("input", () => {
    num.value = range.value;
    update(range.value);
  });
  num.addEventListener("input", () => {
    if (num.value !== "" && !Number.isNaN(Number(num.value))) {
      range.value = num.value;
      update(num.value);
    }
  });
}

async function refreshWorkspaceFiles() {
  try {
    const data = await engine.listWorkspaceFiles();
    const select = el("workspace-file-select");
    const placeholder = createNode("option", "", t(state.lang, "source.workspacePlaceholder"));
    placeholder.value = "";
    select.replaceChildren(
      placeholder,
      ...(data.files || []).map((item) => {
        const opt = createNode("option", "", item.path);
        opt.value = item.path;
        return opt;
      })
    );
  } catch {
    // Non-fatal
  }
}

async function loadNewActiveSource(assetSummary) {
  state.activeSource = assetSummary;
  state.cleanResult = null;
  state.masterResult = null;
  state.cutResult = null;
  setAuditionSlot("A", false);
  renderAll();
}

async function handleFileUpload(file) {
  if (!file) return;
  clearAlert();
  setBusy(true, "status.uploading");
  try {
    await loadNewActiveSource(await engine.uploadFile(file));
  } catch (err) {
    showAlert(t(state.lang, "error.validationTitle"), err.message);
  } finally {
    setBusy(false);
  }
}

function bindEvents() {
  el("clean-form").noValidate = true;
  el("master-form").noValidate = true;
  el("cut-form").noValidate = true;

  el("lang-en").addEventListener("click", () => applyLanguage("en"));
  el("lang-fr").addEventListener("click", () => applyLanguage("fr"));
  el("btn-dismiss-alert").addEventListener("click", clearAlert);

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  document.querySelectorAll(".view-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".view-btn").forEach((b) => b.classList.toggle("active", b === btn));
      waveform.setViewMode(btn.dataset.view);
    });
  });

  syncRangeAndNumber("clean-highpass-range", "clean-highpass-hz", "clean-hpf-readout", "Hz", 0);
  syncRangeAndNumber("clean-strength-range", "clean-denoise-strength", "clean-strength-readout", "", 2);
  syncRangeAndNumber("clean-floor-range", "clean-spectral-floor", "clean-floor-readout", "dB", 0);
  syncRangeAndNumber("cut-thresh-range", "cut-thresh-db", "cut-thresh-readout", "dB", 0, (val) => {
    waveform.setThresholdDb(val);
  });

  el("cut-fps").addEventListener("change", () => {
    state.fps = Number(el("cut-fps").value) || 25;
    updateTransportReadout();
    renderCutTab();
  });

  el("ribbon-preset-select").addEventListener("change", (evt) => selectPreset(evt.target.value));

  el("btn-audition-a").addEventListener("click", () => setAuditionSlot("A", false));
  el("btn-audition-b").addEventListener("click", () => setAuditionSlot("B", false));

  const player = el("ab-audio-player");
  player.addEventListener("timeupdate", updateTransportReadout);
  player.addEventListener("loadedmetadata", updateTransportReadout);
  player.addEventListener("play", startLiveMeters);
  player.addEventListener("ended", () => {
    updateTransportReadout();
    renderMeterBridge();
  });
  player.addEventListener("pause", () => renderMeterBridge());

  el("btn-rewind").addEventListener("click", () => {
    player.currentTime = 0;
    updateTransportReadout();
  });
  el("btn-play-a").addEventListener("click", () => setAuditionSlot("A", true));
  el("btn-play-b").addEventListener("click", () => setAuditionSlot("B", true));
  el("btn-stop").addEventListener("click", () => {
    player.pause();
    player.currentTime = 0;
    updateTransportReadout();
    renderMeterBridge();
  });
  el("btn-loop").addEventListener("click", () => {
    state.looping = !state.looping;
    player.loop = state.looping;
    el("btn-loop").classList.toggle("active", state.looping);
    el("btn-loop").setAttribute("aria-pressed", String(state.looping));
  });

  el("btn-load-sample").addEventListener("click", async () => {
    clearAlert();
    setBusy(true, "status.loadingSample");
    try {
      await loadNewActiveSource(await engine.loadSample());
    } catch (err) {
      showAlert(t(state.lang, "error.validationTitle"), err.message);
    } finally {
      setBusy(false);
    }
  });

  el("audio-upload-input").addEventListener("change", (evt) => {
    const file = evt.target.files && evt.target.files[0];
    if (file) handleFileUpload(file);
    evt.target.value = "";
  });

  const subbar = document.querySelector(".daw-subbar");
  subbar.addEventListener("dragover", (evt) => {
    evt.preventDefault();
    subbar.classList.add("drag-active");
  });
  subbar.addEventListener("dragleave", () => subbar.classList.remove("drag-active"));
  subbar.addEventListener("drop", (evt) => {
    evt.preventDefault();
    subbar.classList.remove("drag-active");
    const file = evt.dataTransfer && evt.dataTransfer.files && evt.dataTransfer.files[0];
    if (file) handleFileUpload(file);
  });

  el("btn-refresh-ws").addEventListener("click", refreshWorkspaceFiles);
  el("btn-load-workspace").addEventListener("click", async () => {
    clearAlert();
    const relPath = el("workspace-file-select").value;
    if (!relPath) {
      showAlert(t(state.lang, "error.validationTitle"), t(state.lang, "error.noWorkspaceFile"));
      return;
    }
    setBusy(true, "status.loadingWorkspace");
    try {
      await loadNewActiveSource(await engine.loadWorkspaceFile(relPath));
    } catch (err) {
      showAlert(t(state.lang, "error.validationTitle"), err.message);
    } finally {
      setBusy(false);
    }
  });

  el("btn-reset-session").addEventListener("click", async () => {
    clearAlert();
    try {
      await engine.resetSession();
      state.activeSource = null;
      state.cleanResult = null;
      state.masterResult = null;
      state.cutResult = null;
      setAuditionSlot("A", false);
      renderAll();
    } catch (err) {
      showAlert(t(state.lang, "error.validationTitle"), err.message);
    }
  });

  el("btn-run-probe").addEventListener("click", async () => {
    clearAlert();
    if (!state.activeSource) {
      showAlert(t(state.lang, "error.validationTitle"), t(state.lang, "error.noSource"));
      return;
    }
    setBusy(true, "status.probing");
    try {
      state.activeSource = await engine.probe(state.activeSource.asset_id, Number(el("probe-clip-threshold").value));
      renderAll();
    } catch (err) {
      showAlert(t(state.lang, "error.validationTitle"), err.message);
    } finally {
      setBusy(false);
    }
  });

  el("btn-download-probe-json").addEventListener("click", () => {
    if (!state.activeSource) return;
    const blob = new Blob([JSON.stringify(state.activeSource.probe, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${state.activeSource.filename}_probe.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  el("clean-form").addEventListener("submit", async (evt) => {
    evt.preventDefault();
    clearAlert();
    if (!state.activeSource) {
      showAlert(t(state.lang, "error.validationTitle"), t(state.lang, "error.noSource"));
      return;
    }
    setBusy(true, "status.cleaning");
    try {
      state.cleanResult = await engine.clean({
        asset_id: state.activeSource.asset_id,
        highpass_hz: Number(el("clean-highpass-hz").value),
        denoise_strength: Number(el("clean-denoise-strength").value),
        spectral_floor_db: Number(el("clean-spectral-floor").value),
        n_fft: Number(el("clean-n-fft").value),
        hop_length: Number(el("clean-hop-length").value),
        declick: el("clean-declick").checked,
        remove_dc: el("clean-remove-dc").checked,
        deess_enabled: el("clean-deess").checked,
        gate_enabled: el("clean-gate").checked,
      });
      setAuditionSlot("B", false);
      renderAll();
    } catch (err) {
      showAlert(t(state.lang, "error.validationTitle"), err.message);
    } finally {
      setBusy(false);
    }
  });

  el("master-form").addEventListener("submit", async (evt) => {
    evt.preventDefault();
    clearAlert();
    if (!state.activeSource) {
      showAlert(t(state.lang, "error.validationTitle"), t(state.lang, "error.noSource"));
      return;
    }
    setBusy(true, "status.mastering");
    try {
      const rawLufs = el("master-target-lufs").value.trim();
      const rawLra = el("master-target-lra").value.trim();
      state.masterResult = await engine.master({
        asset_id: state.activeSource.asset_id,
        preset: state.selectedPreset,
        target_lufs: rawLufs === "" ? null : Number(rawLufs),
        max_true_peak_dbtp: Number(el("master-max-tp").value),
        target_lra: rawLra === "" ? null : Number(rawLra),
        format: el("master-format").value,
        bitrate: el("master-bitrate").value,
        dual_mono: el("master-dual-mono").checked,
      });
      setAuditionSlot("B", false);
      renderAll();
    } catch (err) {
      showAlert(t(state.lang, "error.validationTitle"), err.message);
    } finally {
      setBusy(false);
    }
  });

  el("cut-form").addEventListener("submit", async (evt) => {
    evt.preventDefault();
    clearAlert();
    if (!state.activeSource) {
      showAlert(t(state.lang, "error.validationTitle"), t(state.lang, "error.noSource"));
      return;
    }
    setBusy(true, "status.cutting");
    try {
      state.cutResult = await engine.cut({
        asset_id: state.activeSource.asset_id,
        silence_thresh_db: Number(el("cut-thresh-db").value),
        min_silence_ms: Number(el("cut-min-silence").value),
        min_speech_ms: Number(el("cut-min-speech").value),
        pad_ms: Number(el("cut-pad-ms").value),
        fps: Number(el("cut-fps").value),
        render_audio: el("cut-render-audio").checked,
      });
      setAuditionSlot(state.cutResult.trimmed_asset ? "B" : "A", false);
      renderAll();
    } catch (err) {
      showAlert(t(state.lang, "error.validationTitle"), err.message);
    } finally {
      setBusy(false);
    }
  });

  el("btn-clean-play-a").addEventListener("click", () => setAuditionSlot("A", true));
  el("btn-clean-play-b").addEventListener("click", () => setAuditionSlot("B", true));
  el("btn-master-play-a").addEventListener("click", () => setAuditionSlot("A", true));
  el("btn-master-play-b").addEventListener("click", () => setAuditionSlot("B", true));
  el("btn-cut-play-a").addEventListener("click", () => setAuditionSlot("A", true));
  el("btn-cut-play-b").addEventListener("click", () => setAuditionSlot("B", true));

  const activateAsset = async (asset) => {
    if (!asset) return;
    clearAlert();
    try {
      const snap = await engine.activateSource(asset.asset_id);
      state.activeSource = snap.active_source;
      state.cleanResult = snap.clean;
      state.masterResult = snap.master;
      state.cutResult = snap.cut;
      setAuditionSlot("A", false);
      renderAll();
    } catch (err) {
      showAlert(t(state.lang, "error.validationTitle"), err.message);
    }
  };

  el("btn-use-cleaned").addEventListener("click", () => {
    if (state.cleanResult) activateAsset(state.cleanResult.output_asset);
  });
  el("btn-use-mastered").addEventListener("click", () => {
    if (state.masterResult) activateAsset(state.masterResult.output_asset);
  });
}

async function init() {
  buildLedColumns();
  bindEvents();

  const { mode, health, session } = await engine.boot();
  el("studio-version-badge").textContent = `v${health.version}`;
  el("workspace-dir-pill").textContent = health.workspace_dir;
  el("workspace-dir-pill").title = health.workspace_dir;

  const modeBadge = el("engine-mode-badge");
  modeBadge.textContent = mode === "static" ? "BROWSER WEBAUDIO DEMO" : "LOCAL PYTHON+FFMPEG";
  modeBadge.classList.toggle("demo", mode === "static");

  state.presets = health.presets || {};
  renderPresetPills();
  selectPreset(state.selectedPreset);

  state.activeSource = session.active_source || null;
  state.cleanResult = session.clean || null;
  state.masterResult = session.master || null;
  state.cutResult = session.cut || null;

  await refreshWorkspaceFiles();
  applyLanguage(state.lang);
  switchTab(state.activeTab);
  setAuditionSlot("A", false);
  renderAll();
}

init();
