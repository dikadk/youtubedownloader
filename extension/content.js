(() => {
  const BACKEND = "http://127.0.0.1:5005";
  let panel;

  function getVideoId() {
    const u = new URL(location.href);
    return u.searchParams.get("v");
  }

  function ensurePanel() {
    if (panel && document.body.contains(panel)) return panel;
    panel = document.createElement("div");
    panel.id = "ytmp3-overlay";
    panel.innerHTML = `
      <button class="ytmp3-close" title="Hide">×</button>
      <div class="ytmp3-title">YT → Audio</div>
      <select class="ytmp3-fmt">
        <option value="mp3">MP3 (192k)</option>
        <option value="flac">FLAC (lossless)</option>
        <option value="m4a">M4A (passthrough)</option>
        <option value="opus">Opus (passthrough)</option>
      </select>
      <button class="ytmp3-btn">⬇ Download</button>
      <div class="ytmp3-status"></div>
    `;
    document.body.appendChild(panel);
    panel.querySelector(".ytmp3-close").addEventListener("click", () => panel.remove());
    panel.querySelector(".ytmp3-btn").addEventListener("click", startDownload);
    chrome.storage?.local.get("fmt", ({fmt}) => {
      if (fmt) panel.querySelector(".ytmp3-fmt").value = fmt;
    });
    panel.querySelector(".ytmp3-fmt").addEventListener("change", (e) => {
      chrome.storage?.local.set({fmt: e.target.value});
    });
    return panel;
  }

  async function startDownload() {
    const id = getVideoId();
    if (!id) return setStatus("No video on this page.", true);
    const fmt = panel.querySelector(".ytmp3-fmt").value;
    const btn = panel.querySelector(".ytmp3-btn");
    btn.disabled = true; btn.textContent = "Queued...";
    setStatus("");
    try {
      const r = await fetch(BACKEND + "/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id, format: fmt })
      });
      const j = await r.json();
      if (j.error) throw new Error(j.error);
      poll(j.job_id, btn);
    } catch (e) {
      setStatus("Backend not reachable. Start app.py. (" + e.message + ")", true);
      btn.disabled = false; btn.textContent = "⬇ Download";
    }
  }

  async function poll(jobId, btn) {
    try {
      const r = await fetch(BACKEND + "/status/" + jobId);
      const s = await r.json();
      if (s.status === "done") {
        const url = BACKEND + "/file/" + jobId;
        chrome.runtime.sendMessage({ kind: "download", url });
        setStatus(`Done. <a class="ytmp3-link" href="${url}" target="_blank">Open</a>`);
        btn.disabled = false; btn.textContent = "⬇ Download";
        return;
      }
      if (s.status === "error") {
        setStatus("Error: " + (s.error || "failed"), true);
        btn.disabled = false; btn.textContent = "⬇ Download";
        return;
      }
      btn.textContent = s.status === "processing" ? "Encoding..." : `Downloading ${s.progress || 0}%`;
      setTimeout(() => poll(jobId, btn), 700);
    } catch (e) {
      setStatus("Lost backend: " + e.message, true);
      btn.disabled = false; btn.textContent = "⬇ Download";
    }
  }

  function setStatus(html, isErr) {
    const el = panel.querySelector(".ytmp3-status");
    el.className = "ytmp3-status" + (isErr ? " ytmp3-err" : "");
    el.innerHTML = html;
  }

  function init() {
    if (!getVideoId()) return;
    ensurePanel();
  }

  // YouTube SPA — re-run on URL changes
  let lastUrl = location.href;
  new MutationObserver(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      setTimeout(init, 400);
    }
  }).observe(document, { subtree: true, childList: true });

  init();
})();
