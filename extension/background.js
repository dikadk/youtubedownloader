chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.kind === "download" && msg.url) {
    chrome.downloads.download({ url: msg.url, saveAs: true });
  }
});
