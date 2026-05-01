import os
import re
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory, abort
from yt_dlp import YoutubeDL

BASE = Path(__file__).parent
DOWNLOADS = BASE / "downloads"
DOWNLOADS.mkdir(exist_ok=True)

app = Flask(__name__)


@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/<path:_p>", methods=["OPTIONS"])
@app.route("/", methods=["OPTIONS"])
def _opts(_p=None):
    return ("", 204)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def search_youtube(query: str, limit: int = 10) -> list[dict]:
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "default_search": f"ytsearch{limit}",
        "noplaylist": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
    entries = info.get("entries", []) if info else []
    results = []
    for e in entries:
        if not e:
            continue
        vid = e.get("id")
        if not vid:
            continue
        results.append({
            "id": vid,
            "title": e.get("title") or "",
            "uploader": e.get("uploader") or e.get("channel") or "",
            "duration": e.get("duration"),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url")
                          if e.get("thumbnails") else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        })
    return results


FORMATS = {
    "mp3":  {"codec": "mp3",  "quality": "192", "ext": ".mp3"},
    "flac": {"codec": "flac", "quality": "0",   "ext": ".flac"},
    "m4a":  {"codec": "m4a",  "quality": "0",   "ext": ".m4a"},
    "opus": {"codec": "opus", "quality": "0",   "ext": ".opus"},
}


def run_download(job_id: str, video_url: str, fmt: str = "mp3"):
    job = JOBS[job_id]
    spec = FORMATS.get(fmt, FORMATS["mp3"])

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0
            job["progress"] = round(pct, 1)
            job["status"] = "downloading"
        elif d.get("status") == "finished":
            job["status"] = "processing"
            job["progress"] = 99.0

    out_template = str(DOWNLOADS / f"{job_id}-%(title)s.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": True,
        "progress_hooks": [hook],
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": spec["codec"],
            "preferredquality": spec["quality"],
        }],
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
        # filename ends as .mp3 after postprocess
        title = info.get("title", "audio")
        for f in DOWNLOADS.iterdir():
            if f.name.startswith(job_id) and f.suffix == spec["ext"]:
                job["filename"] = f.name
                break
        job["title"] = title
        job["status"] = "done"
        job["progress"] = 100.0
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "missing q"}), 400
    try:
        results = search_youtube(q, limit=10)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"results": results})


@app.route("/stream/<vid>")
def stream(vid):
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,15}", vid):
        return jsonify({"error": "invalid id"}), 400
    try:
        with YoutubeDL({"quiet": True, "skip_download": True, "format": "bestaudio/best"}) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        url = info.get("url")
        if not url:
            for f in info.get("formats", []):
                if f.get("acodec") and f.get("acodec") != "none":
                    url = f.get("url")
                    break
        if not url:
            return jsonify({"error": "no audio stream"}), 404
        return jsonify({"url": url, "title": info.get("title", "")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json(silent=True) or {}
    vid = (data.get("id") or "").strip()
    fmt = (data.get("format") or "mp3").lower()
    if fmt not in FORMATS:
        return jsonify({"error": f"format must be one of {list(FORMATS)}"}), 400
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,15}", vid):
        return jsonify({"error": "invalid id"}), 400
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "progress": 0.0, "format": fmt}
    url = f"https://www.youtube.com/watch?v={vid}"
    threading.Thread(target=run_download, args=(job_id, url, fmt), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job)


@app.route("/file/<job_id>")
def file(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        abort(404)
    fname = job.get("filename")
    if not fname:
        abort(404)
    return send_from_directory(DOWNLOADS, fname, as_attachment=True)


def run_server():
    app.run(host="127.0.0.1", port=5005, debug=False, use_reloader=False)


if __name__ == "__main__":
    import sys
    if "--web" in sys.argv:
        app.run(host="127.0.0.1", port=5005, debug=True)
    else:
        import webview
        threading.Thread(target=run_server, daemon=True).start()
        webview.create_window("YT MP3", "http://127.0.0.1:5005", width=900, height=720)
        webview.start()
