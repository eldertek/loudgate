import { StaticDemoEngine } from "./demo_dsp.js";

async function serverJson(relUrl, options = {}) {
  const resp = await fetch(relUrl, options);
  let data = null;
  try {
    data = await resp.json();
  } catch {
    data = null;
  }
  if (!resp.ok) {
    const msg = data && data.error ? data.error : `HTTP ${resp.status}`;
    throw new Error(msg);
  }
  return data;
}

function toRel(url) {
  if (!url) return url;
  if (url.startsWith("blob:") || url.startsWith("http")) return url;
  return url.startsWith("/") ? `.${url}` : url;
}

function normalizeAssetUrls(asset) {
  if (!asset) return asset;
  return {
    ...asset,
    audio_url: toRel(asset.audio_url),
    download_url: toRel(asset.download_url),
  };
}

function normalizeStatePayload(payload) {
  if (!payload) return payload;
  const out = { ...payload };
  if (out.active_source) out.active_source = normalizeAssetUrls(out.active_source);
  if (out.output_asset) out.output_asset = normalizeAssetUrls(out.output_asset);
  if (out.trimmed_asset) out.trimmed_asset = normalizeAssetUrls(out.trimmed_asset);
  if (out.clean) out.clean = normalizeStatePayload(out.clean);
  if (out.master) out.master = normalizeStatePayload(out.master);
  if (out.cut) out.cut = normalizeStatePayload(out.cut);
  if (out.downloads) {
    const dl = {};
    for (const [k, v] of Object.entries(out.downloads)) dl[k] = toRel(v);
    out.downloads = dl;
  }
  return out;
}

export class StudioEngine {
  constructor() {
    this.mode = "server";
    this.demo = new StaticDemoEngine();
  }

  async boot() {
    try {
      const health = await serverJson("./api/health");
      if (health && health.status === "ok") {
        this.mode = "server";
        const session = normalizeStatePayload(await serverJson("./api/session"));
        return { mode: "server", health, session };
      }
    } catch {
      // Static web deployment fallback (e.g. eclipse-technology.eu/demos/loudgate/)
    }
    this.mode = "static";
    const health = await this.demo.health();
    const session = await this.demo.getSession();
    return { mode: "static", health, session };
  }

  async listWorkspaceFiles() {
    if (this.mode === "static") return this.demo.listWorkspaceFiles();
    return serverJson("./api/workspace/files");
  }

  async loadSample() {
    if (this.mode === "static") return this.demo.loadSample();
    return normalizeAssetUrls(
      await serverJson("./api/sample", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      })
    );
  }

  async uploadFile(file) {
    if (this.mode === "static") return this.demo.uploadFile(file);
    const form = new FormData();
    form.append("file", file, file.name);
    return normalizeAssetUrls(await serverJson("./api/upload", { method: "POST", body: form }));
  }

  async loadWorkspaceFile(relPath) {
    if (this.mode === "static") throw new Error("Workspace file browser requires local server mode.");
    return normalizeAssetUrls(
      await serverJson("./api/workspace/load", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: relPath }),
      })
    );
  }

  async resetSession() {
    if (this.mode === "static") return this.demo.resetSession();
    return normalizeStatePayload(
      await serverJson("./api/session/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      })
    );
  }

  async activateSource(assetId) {
    if (this.mode === "static") return this.demo.activateSource(assetId);
    return normalizeStatePayload(
      await serverJson("./api/session/activate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asset_id: assetId }),
      })
    );
  }

  async probe(assetId, clipThreshold) {
    if (this.mode === "static") return this.demo.probe(assetId, clipThreshold);
    return normalizeAssetUrls(
      await serverJson("./api/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asset_id: assetId, clip_threshold: clipThreshold }),
      })
    );
  }

  async clean(params) {
    if (this.mode === "static") return this.demo.clean(params);
    return normalizeStatePayload(
      await serverJson("./api/clean", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(params),
      })
    );
  }

  async master(params) {
    if (this.mode === "static") return this.demo.master(params);
    return normalizeStatePayload(
      await serverJson("./api/master", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(params),
      })
    );
  }

  async cut(params) {
    if (this.mode === "static") return this.demo.cut(params);
    return normalizeStatePayload(
      await serverJson("./api/cut", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(params),
      })
    );
  }
}
