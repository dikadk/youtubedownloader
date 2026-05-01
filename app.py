import io
import json
import os
import re
import threading
import urllib.request
import uuid
import zipfile
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory, abort, Response
from yt_dlp import YoutubeDL

BASE = Path(__file__).parent
DEFAULT_DOWNLOADS = BASE / "downloads"
CONFIG_PATH = BASE / "config.json"
STAGING_NAME = ".staging"

app = Flask(__name__)


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def _resolve_dir(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def current_download_dir() -> Path:
    cfg = _load_config()
    candidate = cfg.get("download_dir") or os.environ.get("YTMP3_DOWNLOAD_DIR") or str(DEFAULT_DOWNLOADS)
    d = _resolve_dir(candidate)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = _resolve_dir(str(DEFAULT_DOWNLOADS))
        d.mkdir(parents=True, exist_ok=True)
    return d


def staging_dir() -> Path:
    s = current_download_dir() / STAGING_NAME
    s.mkdir(parents=True, exist_ok=True)
    return s


def _unique_path(directory: Path, base: str, ext: str) -> Path:
    base = base.strip() or "track"
    candidate = directory / f"{base}{ext}"
    i = 2
    while candidate.exists():
        candidate = directory / f"{base} ({i}){ext}"
        i += 1
    return candidate


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
PLAYLISTS: dict[str, dict] = {}
PLAYLISTS_LOCK = threading.Lock()

SPOTIFY_PLAYLIST_RE = re.compile(r"(?:open\.spotify\.com/(?:embed/)?playlist/|spotify:playlist:)([A-Za-z0-9]+)")


def _safe_name(s: str, maxlen: int = 80) -> str:
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", s or "").strip(" .")
    return (s[:maxlen] or "track").strip()


def fetch_spotify_playlist(url_or_id: str) -> dict:
    m = SPOTIFY_PLAYLIST_RE.search(url_or_id) if "/" in url_or_id or ":" in url_or_id else None
    pid = m.group(1) if m else url_or_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9]{16,40}", pid):
        raise ValueError("invalid spotify playlist id")
    embed = f"https://open.spotify.com/embed/playlist/{pid}"
    req = urllib.request.Request(embed, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="replace")
    m2 = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.S)
    if not m2:
        raise RuntimeError("could not parse spotify playlist (embed format changed)")
    data = json.loads(m2.group(1))
    entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {}) or {}
    tracks_raw = entity.get("trackList") or []
    tracks = []
    for t in tracks_raw:
        title = (t.get("title") or "").strip()
        artists = (t.get("subtitle") or "").replace("\xa0", " ").strip()
        if not title:
            continue
        tracks.append({
            "title": title,
            "artists": artists,
            "duration_ms": t.get("duration"),
            "uri": t.get("uri"),
            "query": f"{artists} - {title}".strip(" -"),
        })
    return {
        "id": pid,
        "name": entity.get("name") or "Spotify Playlist",
        "tracks": tracks,
    }


def run_playlist_download(pl_id: str, fmt: str):
    pl = PLAYLISTS[pl_id]
    spec = FORMATS.get(fmt, FORMATS["mp3"])
    pl["status"] = "running"
    for idx, item in enumerate(pl["tracks"]):
        if pl.get("cancelled"):
            break
        item["status"] = "searching"
        try:
            results = search_youtube(item["query"], limit=1)
            if not results:
                raise RuntimeError("no youtube match")
            vid = results[0]["id"]
            item["youtube_id"] = vid
            item["youtube_title"] = results[0]["title"]
            sub_id = f"{pl_id}-{idx:03d}"
            with JOBS_LOCK:
                JOBS[sub_id] = {"status": "queued", "progress": 0.0, "format": fmt}
            item["job_id"] = sub_id
            item["status"] = "downloading"
            final_name = f"{item.get('artists','')} - {item['title']}".strip(" -")
            run_download(sub_id, f"https://www.youtube.com/watch?v={vid}", fmt, final_name=final_name)
            sub = JOBS[sub_id]
            if sub.get("status") == "done":
                item["status"] = "done"
                item["filename"] = sub.get("filename")
                item["path"] = sub.get("path")
                pl["completed"] += 1
            else:
                item["status"] = "error"
                item["error"] = sub.get("error", "download failed")
                pl["failed"] += 1
        except Exception as e:
            item["status"] = "error"
            item["error"] = str(e)
            pl["failed"] += 1
        pl["progress"] = round((idx + 1) / max(1, len(pl["tracks"])) * 100, 1)
    pl["status"] = "done"


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


def run_download(job_id: str, video_url: str, fmt: str = "mp3", *, final_name: str | None = None):
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

    staging = staging_dir()
    out_template = str(staging / f"{job_id}.%(ext)s")
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
        title = info.get("title", "audio")
        staged = None
        for f in staging.iterdir():
            if f.stem == job_id and f.suffix == spec["ext"]:
                staged = f
                break
        if staged is None:
            raise RuntimeError(f"staged file not found for job {job_id}")
        clean_base = _safe_name(final_name or title or "audio")
        target_dir = current_download_dir()
        final_path = _unique_path(target_dir, clean_base, spec["ext"])
        staged.replace(final_path)
        job["filename"] = final_path.name
        job["path"] = str(final_path)
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
    p = job.get("path")
    if p and Path(p).exists():
        path = Path(p)
        return send_from_directory(path.parent, path.name, as_attachment=True)
    fname = job.get("filename")
    if fname and (current_download_dir() / fname).exists():
        return send_from_directory(current_download_dir(), fname, as_attachment=True)
    abort(404)


@app.route("/pick_folder", methods=["POST"])
def pick_folder():
    try:
        import webview
        wins = webview.windows
        if not wins:
            return jsonify({"error": "no webview window (run as desktop app)"}), 400
        result = wins[0].create_file_dialog(webview.FOLDER_DIALOG)
    except ImportError:
        return jsonify({"error": "pywebview not available"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not result:
        return jsonify({"cancelled": True})
    path = result[0] if isinstance(result, (list, tuple)) else result
    return jsonify({"path": str(path)})


@app.route("/config", methods=["GET"])
def config_get():
    return jsonify({
        "download_dir": str(current_download_dir()),
        "default": str(_resolve_dir(str(DEFAULT_DOWNLOADS))),
        "env": os.environ.get("YTMP3_DOWNLOAD_DIR"),
    })


@app.route("/config", methods=["POST"])
def config_set():
    data = request.get_json(silent=True) or {}
    raw = (data.get("download_dir") or "").strip()
    if not raw and data.get("reset"):
        cfg = _load_config()
        cfg.pop("download_dir", None)
        _save_config(cfg)
        return jsonify({"download_dir": str(current_download_dir())})
    if not raw:
        return jsonify({"error": "missing download_dir"}), 400
    try:
        d = _resolve_dir(raw)
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".ytmp3_write_test"
        probe.write_text("ok")
        probe.unlink()
    except Exception as e:
        return jsonify({"error": f"path not writable: {e}"}), 400
    cfg = _load_config()
    cfg["download_dir"] = str(d)
    _save_config(cfg)
    return jsonify({"download_dir": str(d)})


@app.route("/playlist", methods=["GET"])
def playlist_info():
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing url"}), 400
    try:
        pl = fetch_spotify_playlist(url)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(pl)


@app.route("/playlist/download", methods=["POST"])
def playlist_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    fmt = (data.get("format") or "mp3").lower()
    if fmt not in FORMATS:
        return jsonify({"error": f"format must be one of {list(FORMATS)}"}), 400
    if not url:
        return jsonify({"error": "missing url"}), 400
    try:
        pl = fetch_spotify_playlist(url)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    pl_id = "pl" + uuid.uuid4().hex[:10]
    with PLAYLISTS_LOCK:
        PLAYLISTS[pl_id] = {
            "id": pl_id,
            "name": pl["name"],
            "spotify_id": pl["id"],
            "format": fmt,
            "status": "queued",
            "progress": 0.0,
            "completed": 0,
            "failed": 0,
            "tracks": [dict(t, status="queued") for t in pl["tracks"]],
        }
    threading.Thread(target=run_playlist_download, args=(pl_id, fmt), daemon=True).start()
    return jsonify({"playlist_id": pl_id, "name": pl["name"], "count": len(pl["tracks"])})


@app.route("/playlist/status/<pl_id>")
def playlist_status(pl_id):
    pl = PLAYLISTS.get(pl_id)
    if not pl:
        return jsonify({"error": "unknown playlist"}), 404
    return jsonify(pl)


@app.route("/playlist/zip/<pl_id>")
def playlist_zip(pl_id):
    pl = PLAYLISTS.get(pl_id)
    if not pl:
        abort(404)
    files = []
    for t in pl["tracks"]:
        p = t.get("path")
        if p and Path(p).exists():
            files.append((t, Path(p)))
    if not files:
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        used = set()
        for t, path in files:
            base = _safe_name(f"{t.get('artists','')} - {t['title']}")
            ext = path.suffix
            name = base + ext
            i = 1
            while name in used:
                name = f"{base} ({i}){ext}"
                i += 1
            used.add(name)
            zf.write(path, arcname=name)
    buf.seek(0)
    fname = _safe_name(pl["name"]) + ".zip"
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


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
