import { t } from "./i18n.js";
import { WaveformRenderer } from "./waveform.js";

const STORAGE_LANG_KEY = "loudgate_ui_lang";
const STORAGE_TAB_KEY = "loudgate_ui_tab";

const state = {
  lang: localStorage.getItem(STORAGE_LANG_KEY) === "fr" ? "fr" : "en",
  activeTab: localStorage.getItem(STORAGE_TAB_KEY) || "probe",
  presets: {},
  selectedPreset: "podcast",
  activeSource: null,
  cleanResult: null,
  masterResult: null,
  cutResult: null,
  auditionSlot: "A", // "A" = original source, "B" = processed output for active tab
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

function fmtTime(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  const mins = Math.floor(s / 60);
  const rem = s - mins * 60;
  return `${String(mins).padStart(2, "0")}:${rem.toFixed(2).padStart(5, "0")}`;
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
    el("status-toast-text").textContent = t(state.lang, messageKey) || "Working...";
    toast.classList.remove("hidden");
  } else {
    toast.classList.add("hidden");
  }
  const actionIds = [
    "btn-load-sample",
    "btn-load-workspace",
    "btn-run-probe",
    "btn-run-clean",
    "btn-run-master",
    "btn-run-cut",
  ];
  for (const id of actionIds) {
    const btn = el(id);
    if (btn) {
      btn.disabled = busy || (!state.activeSource && id.startsWith("btn-run-"));
    }
  }
}

async function apiJson(url, options = {}) {
  const resp = await fetch(url, options);
  let data = null;
  try {
    data = await resp.json();
  } catch {
    data = null;
  }
  if (!resp.ok) {
    const msg = (data && data.error) ? data.error : `HTTP ${resp.status}`;
    throw new Error(msg);
  }
  return data;
}

const waveform = new WaveformRenderer(
  el("waveform-canvas"),
  (seekTimeS) => {
    const player = el("ab-audio-player");
    if (!player || !player.src) return;
    const dur = Number.isFinite(player.duration) && player.duration > 0
      ? player.duration
      : (state.activeSource ? state.activeSource.probe.duration_s : 0);
    if (dur > 0) {
      player.currentTime = Math.max(0, Math.min(dur, seekTimeS));
      updateTransportReadout();
    }
  },
  (hoverTimeS) => {
    el("waveform-cursor-info").textContent = fmtTime(hoverTimeS);
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
    if (key) {
      node.textContent = t(state.lang, key);
    }
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
  if (slot === "B" && !bInfo) {
    state.auditionSlot = "A";
  } else {
    state.auditionSlot = slot;
  }

  const targetAsset = state.auditionSlot === "B" && bInfo ? bInfo.asset : state.activeSource;
  el("btn-audition-a").classList.toggle("active", state.auditionSlot === "A");
  el("btn-audition-b").classList.toggle("active", state.auditionSlot === "B");

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
}

function updateTransportReadout() {
  const player = el("ab-audio-player");
  const cur = player.currentTime || 0;
  const dur = Number.isFinite(player.duration) && player.duration > 0
    ? player.duration
    : (state.activeSource ? state.activeSource.probe.duration_s : 0);
  el("transport-time-readout").textContent = `${fmtTime(cur)} / ${fmtTime(dur)}`;
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

  const bInfo = getPreferredBAsset();
  el("btn-audition-b").disabled = !bInfo;
  el("audition-b-label").textContent = t(state.lang, bInfo ? bInfo.labelKey : "transport.auditionB");

  if (!hasSource) {
    const hint = createNode("span", "empty-source-hint", t(state.lang, "source.emptyHint"));
    hint.id = "empty-source-hint";
    metaWrap.replaceChildren(hint);
    return;
  }

  const s = state.activeSource;
  const p = s.probe;
  const kindBadge = createNode("span", "badge badge-indigo", s.source_kind.toUpperCase());
  const nameChip = createNode("span", "source-chip", s.filename);
  const specChip = createNode(
    "span",
    "source-chip",
    `${(p.sample_rate / 1000).toFixed(1)} kHz | ${p.channels}ch | ${p.duration_s.toFixed(2)}s`
  );
  const lufsChip = createNode(
    "span",
    "source-chip",
    `${p.integrated_lufs.toFixed(1)} LUFS | ${p.true_peak_dbtp.toFixed(1)} dBTP`
  );
  metaWrap.replaceChildren(kindBadge, nameChip, specChip, lufsChip);
}

function renderWaveform() {
  const legendKept = el("legend-kept");
  const legendCut = el("legend-cut");
  const trackBadge = el("waveform-track-badge");

  if (!state.activeSource) {
    legendKept.classList.add("hidden");
    legendCut.classList.add("hidden");
    trackBadge.textContent = "--";
    waveform.setData({ waveform: null, emptyMessage: t(state.lang, "waveform.emptyCanvas") });
    return;
  }

  let displayWf = state.activeSource.waveform;
  let cutOverlay = null;
  let badgeKey = "waveform.trackSource";

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
    playheadRatio: 0,
    emptyMessage: t(state.lang, "waveform.emptyCanvas"),
  });
}

function setMeterBar(barId, valId, value, minVal, maxVal, unit, colorClass) {
  const bar = el(barId);
  const label = el(valId);
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    bar.style.width = "0%";
    bar.className = "meter-fill";
    label.textContent = `-- ${unit}`;
    return;
  }
  const pct = Math.max(0, Math.min(100, ((Number(value) - minVal) / (maxVal - minVal)) * 100));
  bar.style.width = `${pct.toFixed(1)}%`;
  bar.className = `meter-fill ${colorClass}`;
  label.textContent = `${Number(value).toFixed(2)} ${unit}`;
}

function renderProbeTab() {
  const grid = el("probe-metrics-grid");
  if (!state.activeSource) {
    setMeterBar("meter-lufs-bar", "meter-lufs-val", null, -60, 0, "LUFS", "meter-green");
    setMeterBar("meter-tp-bar", "meter-tp-val", null, -40, 3, "dBTP", "meter-green");
    setMeterBar("meter-snr-bar", "meter-snr-val", null, 0, 60, "dB", "meter-green");
    grid.replaceChildren();
    return;
  }

  const p = state.activeSource.probe;
  const lufsColor = p.integrated_lufs > -12 ? "meter-amber" : "meter-green";
  const maxTp = Math.max(p.true_peak_dbtp, p.oversampled_true_peak_dbtp);
  const tpColor = maxTp > 0.0 ? "meter-red" : (maxTp > -1.0 ? "meter-amber" : "meter-green");
  const snrColor = p.estimated_snr_db >= 20 ? "meter-green" : (p.estimated_snr_db >= 10 ? "meter-amber" : "meter-red");

  setMeterBar("meter-lufs-bar", "meter-lufs-val", p.integrated_lufs, -60, 0, "LUFS", lufsColor);
  setMeterBar("meter-tp-bar", "meter-tp-val", p.true_peak_dbtp, -40, 3, "dBTP", tpColor);
  setMeterBar("meter-snr-bar", "meter-snr-val", p.estimated_snr_db, 0, 60, "dB", snrColor);

  const tiles = [
    ["Integrated LUFS", fmtNum(p.integrated_lufs, 2, " LUFS"), `Gate: ${fmtNum(p.threshold_lufs, 2, " LUFS")}`],
    ["Loudness Range (LRA)", fmtNum(p.lra_lu, 2, " LU"), `${fmtNum(p.lra_low_lufs, 1)} to ${fmtNum(p.lra_high_lufs, 1)}`],
    ["FFmpeg True Peak", fmtNum(p.true_peak_dbtp, 2, " dBTP"), `Sample: ${fmtNum(p.ffmpeg_sample_peak_dbfs, 2, " dBFS")}`],
    ["4x Oversampled Peak", fmtNum(p.oversampled_true_peak_dbtp, 2, " dBTP"), `ISP Excess: +${fmtNum(p.inter_sample_peak_excess_db, 2, " dB")}`],
    ["Sample Peak / RMS", fmtNum(p.sample_peak_dbfs, 2, " dBFS"), `RMS: ${fmtNum(p.rms_dbfs, 2, " dBFS")}`],
    ["Crest Factor", fmtNum(p.crest_factor_db, 2, " dB"), `SNR: ${fmtNum(p.estimated_snr_db, 1, " dB")}`],
    ["Noise Floor / Signal", fmtNum(p.noise_floor_dbfs, 1, " dBFS"), `Active: ${fmtNum(p.signal_level_dbfs, 1, " dBFS")}`],
    ["Max Abs DC Offset", Number(p.max_abs_dc_offset).toFixed(6), `Ch: ${p.dc_offset_per_channel.map((v) => v.toFixed(4)).join(", ")}`],
    ["Clipped Samples", `${p.clipped_samples}`, `Ratio: ${(p.clipped_ratio * 100).toFixed(4)}%`],
    ["Format & Duration", `${p.sample_rate} Hz / ${p.channels}ch`, `${p.duration_s.toFixed(3)}s (${p.n_samples} smp)`],
  ];

  const nodes = tiles.map(([label, val, sub]) => {
    const card = createNode("div", "metric-tile");
    card.append(
      createNode("div", "metric-tile-label", label),
      createNode("div", "metric-tile-value", val),
      createNode("div", "metric-tile-sub", sub)
    );
    return card;
  });
  grid.replaceChildren(...nodes);
}

function makeDiffCard(label, beforeVal, afterVal, deltaText, deltaClass = "") {
  const card = createNode("div", "diff-card");
  const values = createNode("div", "diff-values");
  values.append(
    createNode("span", "diff-before", beforeVal),
    createNode("span", "diff-after", afterVal)
  );
  const delta = createNode("span", `diff-delta ${deltaClass}`.trim(), deltaText);
  card.append(createNode("div", "diff-title", label), values, delta);
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

  const cards = [
    makeDiffCard(
      "Estimated SNR",
      fmtNum(b.estimated_snr_db, 1, " dB"),
      fmtNum(a.estimated_snr_db, 1, " dB"),
      `${snrDelta >= 0 ? "+" : ""}${snrDelta.toFixed(1)} dB`,
      snrDelta > 0.5 ? "good" : ""
    ),
    makeDiffCard(
      "Noise Floor",
      fmtNum(b.noise_floor_dbfs, 1, " dBFS"),
      fmtNum(a.noise_floor_dbfs, 1, " dBFS"),
      `${floorDelta >= 0 ? "+" : ""}${floorDelta.toFixed(1)} dB`,
      floorDelta < -0.5 ? "good" : ""
    ),
    makeDiffCard(
      "Integrated LUFS",
      fmtNum(b.integrated_lufs, 2, " LUFS"),
      fmtNum(a.integrated_lufs, 2, " LUFS"),
      `${lufsDelta >= 0 ? "+" : ""}${lufsDelta.toFixed(2)} LU`
    ),
    makeDiffCard(
      "Max DC Offset",
      b.max_abs_dc_offset.toFixed(5),
      a.max_abs_dc_offset.toFixed(5),
      `${dcDelta >= 0 ? "+" : ""}${dcDelta.toFixed(5)}`,
      a.max_abs_dc_offset < 1e-4 ? "good" : ""
    ),
    makeDiffCard(
      "True Peak",
      fmtNum(b.true_peak_dbtp, 2, " dBTP"),
      fmtNum(a.true_peak_dbtp, 2, " dBTP"),
      `${tpDelta >= 0 ? "+" : ""}${tpDelta.toFixed(2)} dB`
    ),
  ];
  el("clean-diff-grid").replaceChildren(...cards);

  const statItems = [
    ["Clicks Repaired", String(stats.clicks_repaired)],
    ["Parseval Rel. Error", Number(stats.parseval_relative_error).toExponential(2)],
    ["Est. Noise Reduction", `${Number(stats.noise_reduction_db_est).toFixed(2)} dB`],
    ["FFT / Hop", `${stats.n_fft} / ${stats.hop_length}`],
    ["HPF Cutoff", `${stats.highpass_hz} Hz`],
  ];
  el("clean-dsp-stats").replaceChildren(
    ...statItems.map(([k, v]) => {
      const chip = createNode("span", "stat-chip", `${k}: `);
      chip.append(createNode("strong", "", v));
      return chip;
    })
  );
}

function renderPresetPills() {
  const rack = el("master-presets-rack");
  const entries = Object.entries(state.presets || {}).sort(([a], [b]) => a.localeCompare(b));
  const pills = entries.map(([name, cfg]) => {
    const btn = createNode("button", "preset-pill", `${name} (${cfg.target_lufs} LUFS)`);
    btn.type = "button";
    btn.dataset.preset = name;
    if (name === state.selectedPreset) {
      btn.classList.add("active");
    }
    btn.addEventListener("click", () => {
      state.selectedPreset = name;
      el("master-target-lufs").value = String(cfg.target_lufs);
      el("master-max-tp").value = String(cfg.max_true_peak_dbtp);
      el("master-target-lra").value = String(cfg.target_lra);
      renderPresetPills();
    });
    return btn;
  });
  rack.replaceChildren(...pills);
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

  const compBar = el("master-compliance-bar");
  const badge = createNode(
    "span",
    `badge ${tpOk ? "badge-green" : "badge-amber"}`,
    tpOk
      ? `COMPLIANT: ${a.integrated_lufs.toFixed(2)} LUFS | TP ${a.true_peak_dbtp.toFixed(2)} <= ${mr.max_true_peak_dbtp.toFixed(1)} dBTP`
      : `CHECK CEILING: TP ${a.true_peak_dbtp.toFixed(2)} dBTP`
  );
  const modeSpan = createNode("span", "stat-chip", `Mode: ${mr.loudnorm_mode} | Preset: ${mr.preset}`);
  compBar.replaceChildren(badge, modeSpan);

  const lufsErr = a.integrated_lufs - mr.target_lufs;
  const cards = [
    makeDiffCard(
      "Integrated Loudness",
      fmtNum(b.integrated_lufs, 2, " LUFS"),
      fmtNum(a.integrated_lufs, 2, " LUFS"),
      `Target ${mr.target_lufs.toFixed(1)} (${lufsErr >= 0 ? "+" : ""}${lufsErr.toFixed(2)} LU)`,
      Math.abs(lufsErr) <= 1.0 ? "good" : "warn"
    ),
    makeDiffCard(
      "True Peak (FFmpeg)",
      fmtNum(b.true_peak_dbtp, 2, " dBTP"),
      fmtNum(a.true_peak_dbtp, 2, " dBTP"),
      `Ceiling <= ${mr.max_true_peak_dbtp.toFixed(1)} dBTP`,
      tpOk ? "good" : "warn"
    ),
    makeDiffCard(
      "4x Oversampled TP",
      fmtNum(b.oversampled_true_peak_dbtp, 2, " dBTP"),
      fmtNum(a.oversampled_true_peak_dbtp, 2, " dBTP"),
      `ISP Excess: +${a.inter_sample_peak_excess_db.toFixed(2)} dB`,
      a.oversampled_true_peak_dbtp <= mr.max_true_peak_dbtp + 0.1 ? "good" : "warn"
    ),
    makeDiffCard(
      "Loudness Range (LRA)",
      fmtNum(b.lra_lu, 2, " LU"),
      fmtNum(a.lra_lu, 2, " LU"),
      `Target LRA: ${mr.target_lra.toFixed(1)} LU`
    ),
  ];
  el("master-diff-grid").replaceChildren(...cards);

  const pass1 = mr.pass1_stats || {};
  const stats = [
    ["Pass 1 Input I", `${pass1.input_i || "--"} LUFS`],
    ["Pass 1 Input TP", `${pass1.input_tp || "--"} dBTP`],
    ["Target Offset", `${pass1.target_offset || "--"} dB`],
    ["Output File", outAsset.filename],
  ];
  el("master-pass-stats").replaceChildren(
    ...stats.map(([k, v]) => {
      const chip = createNode("span", "stat-chip", `${k}: `);
      chip.append(createNode("strong", "", v));
      return chip;
    })
  );
}

function setExportLink(id, url, filename) {
  const node = el(id);
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
    return;
  }

  empty.classList.add("hidden");
  body.classList.remove("hidden");
  btnPlayA.disabled = false;
  btnPlayB.disabled = !state.cutResult.trimmed_asset;

  const c = state.cutResult;
  const savedPct = c.input_duration_s > 0 ? (c.removed_duration_s / c.input_duration_s) * 100 : 0;
  const kpis = [
    ["Segments Kept", String(c.segment_count), "VAD speech regions"],
    ["Input Duration", `${c.input_duration_s.toFixed(2)}s`, "Source length"],
    ["Kept Speech", `${c.kept_duration_s.toFixed(2)}s`, `${(100 - savedPct).toFixed(1)}% retained`],
    ["Removed Silence", `${c.removed_duration_s.toFixed(2)}s`, `${savedPct.toFixed(1)}% trimmed`],
  ];
  el("cut-kpi-strip").replaceChildren(
    ...kpis.map(([lbl, val, sub]) => {
      const tile = createNode("div", "metric-tile");
      tile.append(
        createNode("div", "metric-tile-label", lbl),
        createNode("div", "metric-tile-value", val),
        createNode("div", "metric-tile-sub", sub)
      );
      return tile;
    })
  );

  const dl = c.downloads || {};
  setExportLink("export-link-otio", dl.otio);
  setExportLink("export-link-edl", dl.edl);
  setExportLink("export-link-srt", dl.srt);
  setExportLink("export-link-vtt", dl.vtt);
  setExportLink("export-link-wav", dl.wav);

  const tbody = el("cut-segments-tbody");
  if (!c.segments || c.segments.length === 0) {
    const tr = createNode("tr");
    const td = createNode("td", "", "0 active segments detected at this threshold. Lower Silence Threshold (e.g. -48 dBFS).");
    td.colSpan = 6;
    tr.append(td);
    tbody.replaceChildren(tr);
    return;
  }

  const rows = c.segments.map((seg) => {
    const tr = createNode("tr");
    tr.title = `Seek to ${seg.start_s.toFixed(2)}s`;
    tr.append(
      createNode("td", "", `#${seg.index}`),
      createNode("td", "", fmtTime(seg.start_s)),
      createNode("td", "", fmtTime(seg.end_s)),
      createNode("td", "", `${seg.duration_s.toFixed(3)}s`),
      createNode("td", "", `${seg.rms_dbfs.toFixed(1)}`),
      createNode("td", "", `${seg.mean_zcr.toFixed(3)}`)
    );
    tr.addEventListener("click", () => {
      setAuditionSlot("A", false);
      const player = el("ab-audio-player");
      player.currentTime = seg.start_s;
      updateTransportReadout();
    });
    return tr;
  });
  tbody.replaceChildren(...rows);
}

function renderAll() {
  renderSourceDock();
  renderProbeTab();
  renderCleanTab();
  renderMasterTab();
  renderCutTab();
  renderWaveform();
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
}

function syncRangeAndNumber(rangeId, numId, readoutId, unit, decimals = 0) {
  const range = el(rangeId);
  const num = el(numId);
  const readout = el(readoutId);
  const updateReadout = (val) => {
    const formatted = Number(val).toFixed(decimals);
    readout.textContent = unit ? `${formatted} ${unit}` : formatted;
  };
  range.addEventListener("input", () => {
    num.value = range.value;
    updateReadout(range.value);
  });
  num.addEventListener("input", () => {
    if (num.value !== "" && !Number.isNaN(Number(num.value))) {
      range.value = num.value;
      updateReadout(num.value);
    }
  });
}

async function refreshWorkspaceFiles() {
  try {
    const data = await apiJson("/api/workspace/files");
    const select = el("workspace-file-select");
    const placeholder = createNode("option", "", t(state.lang, "source.workspacePlaceholder"));
    placeholder.value = "";
    const options = (data.files || []).map((item) => {
      const opt = createNode("option", "", item.path);
      opt.value = item.path;
      return opt;
    });
    select.replaceChildren(placeholder, ...options);
  } catch {
    // Non-fatal on initial boot
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
    const form = new FormData();
    form.append("file", file, file.name);
    const data = await apiJson("/api/upload", { method: "POST", body: form });
    await loadNewActiveSource(data);
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

  syncRangeAndNumber("clean-highpass-range", "clean-highpass-hz", "clean-hpf-readout", "Hz", 0);
  syncRangeAndNumber("clean-strength-range", "clean-denoise-strength", "clean-strength-readout", "", 2);
  syncRangeAndNumber("clean-floor-range", "clean-spectral-floor", "clean-floor-readout", "dB", 0);
  syncRangeAndNumber("cut-thresh-range", "cut-thresh-db", "cut-thresh-readout", "dB", 0);

  el("btn-audition-a").addEventListener("click", () => setAuditionSlot("A", false));
  el("btn-audition-b").addEventListener("click", () => setAuditionSlot("B", false));

  const player = el("ab-audio-player");
  player.addEventListener("timeupdate", updateTransportReadout);
  player.addEventListener("loadedmetadata", updateTransportReadout);
  player.addEventListener("ended", updateTransportReadout);

  el("btn-load-sample").addEventListener("click", async () => {
    clearAlert();
    setBusy(true, "status.loadingSample");
    try {
      const data = await apiJson("/api/sample", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      await loadNewActiveSource(data);
    } catch (err) {
      showAlert(t(state.lang, "error.validationTitle"), err.message);
    } finally {
      setBusy(false);
    }
  });

  el("audio-upload-input").addEventListener("change", (evt) => {
    const file = evt.target.files && evt.target.files[0];
    if (file) {
      handleFileUpload(file);
    }
    evt.target.value = "";
  });

  const dock = document.querySelector(".source-dock");
  dock.addEventListener("dragover", (evt) => {
    evt.preventDefault();
    dock.classList.add("drag-active");
  });
  dock.addEventListener("dragleave", () => dock.classList.remove("drag-active"));
  dock.addEventListener("drop", (evt) => {
    evt.preventDefault();
    dock.classList.remove("drag-active");
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
      const data = await apiJson("/api/workspace/load", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: relPath }),
      });
      await loadNewActiveSource(data);
    } catch (err) {
      showAlert(t(state.lang, "error.validationTitle"), err.message);
    } finally {
      setBusy(false);
    }
  });

  el("btn-reset-session").addEventListener("click", async () => {
    clearAlert();
    try {
      await apiJson("/api/session/reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
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
      const data = await apiJson("/api/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          asset_id: state.activeSource.asset_id,
          clip_threshold: Number(el("probe-clip-threshold").value),
        }),
      });
      state.activeSource = data;
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
      const data = await apiJson("/api/clean", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
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
        }),
      });
      state.cleanResult = data;
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
      const data = await apiJson("/api/master", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          asset_id: state.activeSource.asset_id,
          preset: state.selectedPreset,
          target_lufs: rawLufs === "" ? null : Number(rawLufs),
          max_true_peak_dbtp: Number(el("master-max-tp").value),
          target_lra: rawLra === "" ? null : Number(rawLra),
          format: el("master-format").value,
          bitrate: el("master-bitrate").value,
          dual_mono: el("master-dual-mono").checked,
        }),
      });
      state.masterResult = data;
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
      const data = await apiJson("/api/cut", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          asset_id: state.activeSource.asset_id,
          silence_thresh_db: Number(el("cut-thresh-db").value),
          min_silence_ms: Number(el("cut-min-silence").value),
          min_speech_ms: Number(el("cut-min-speech").value),
          pad_ms: Number(el("cut-pad-ms").value),
          fps: Number(el("cut-fps").value),
          render_audio: el("cut-render-audio").checked,
        }),
      });
      state.cutResult = data;
      setAuditionSlot(data.trimmed_asset ? "B" : "A", false);
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
      const snap = await apiJson("/api/session/activate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asset_id: asset.asset_id }),
      });
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
  bindEvents();
  try {
    const [health, session] = await Promise.all([
      apiJson("/api/health"),
      apiJson("/api/session"),
    ]);
    el("studio-version-badge").textContent = `Studio v${health.version}`;
    el("workspace-dir-pill").textContent = health.workspace_dir;
    el("workspace-dir-pill").title = health.workspace_dir;
    state.presets = health.presets || {};
    if (state.presets[state.selectedPreset]) {
      const p = state.presets[state.selectedPreset];
      el("master-target-lufs").value = String(p.target_lufs);
      el("master-max-tp").value = String(p.max_true_peak_dbtp);
      el("master-target-lra").value = String(p.target_lra);
    }
    renderPresetPills();

    state.activeSource = session.active_source || null;
    state.cleanResult = session.clean || null;
    state.masterResult = session.master || null;
    state.cutResult = session.cut || null;
  } catch (err) {
    showAlert("Studio Boot Error", err.message);
  }

  await refreshWorkspaceFiles();
  applyLanguage(state.lang);
  switchTab(state.activeTab);
  setAuditionSlot(state.activeSource ? "A" : "A", false);
  renderAll();
}

init();
