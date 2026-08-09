"""
Minimal YouTube downloader web app, with live progress + pause/resume.

Run locally with:
    pip install -r requirements.txt
    python app.py

Then open http://127.0.0.1:5000

Requires ffmpeg installed on your system (for merging video+audio at
resolutions above 360p, since YouTube usually serves high-res video and
audio as separate streams).

How progress/pause works
-------------------------
Downloads run in a background thread per request ("job"). yt-dlp calls a
progress hook frequently while downloading a stream; we store the latest
percent/speed/eta in a shared JOBS dict, keyed by job_id. The frontend polls
/api/download/progress/<job_id> to update the UI.

Pause is implemented by blocking inside that same progress hook whenever a
per-job threading.Event is set — since yt-dlp calls the hook synchronously
from its download loop, blocking there stops it from reading further data
until the event is cleared (Resume). Because yt-dlp downloads with
"continue" enabled by default, even a hard stop/restart would resume from
the partial file, but here pausing is a soft, in-process pause that keeps
the connection open.

Note: for resolutions that need separate video + audio streams merged,
progress will restart (0% -> 100%) twice: once for video, once for audio,
before the final merge step.
"""

import os
import re
import tempfile
import threading
import time
import uuid

from flask import Flask, render_template, request, jsonify, send_file, after_this_request
import yt_dlp

app = Flask(__name__)

QUALITY_MAP = {
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]",
    "Audio only (MP3)": "bestaudio/best",
}

YOUTUBE_URL_RE = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]+"
)

# In-memory job store. Fine for a single-user local app; not meant for
# multi-user / production use.
JOBS = {}
JOBS_LOCK = threading.Lock()


def is_valid_youtube_url(url: str) -> bool:
    return bool(url) and bool(YOUTUBE_URL_RE.match(url.strip()))


@app.route("/")
def index():
    return render_template("index.html", qualities=list(QUALITY_MAP.keys()))


@app.route("/api/info", methods=["POST"])
def video_info():
    data = request.get_json(force=True)
    url = (data or {}).get("url", "").strip()

    if not is_valid_youtube_url(url):
        return jsonify({"error": "That doesn't look like a valid YouTube URL."}), 400

    ydl_opts = {"quiet": True, "skip_download": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": f"Could not read video info: {e}"}), 400

    return jsonify(
        {
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
        }
    )


def _make_progress_hook(job_id):
    def hook(d):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            return

        # Soft pause: block here (inside yt-dlp's own download loop) until resumed.
        while job["pause_event"].is_set():
            time.sleep(0.25)
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if job is None:
                return

        if d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes", 0) or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            speed = d.get("speed") or 0
            eta = d.get("eta")
            percent = round((downloaded / total) * 100, 1) if total else None
            with JOBS_LOCK:
                job.update(
                    {
                        "status": "downloading",
                        "percent": percent,
                        "downloaded_bytes": downloaded,
                        "total_bytes": total,
                        "speed": speed,
                        "eta": eta,
                    }
                )
        elif d.get("status") == "finished":
            with JOBS_LOCK:
                job["status"] = "processing"  # merging / converting

    return hook


def _run_download_job(job_id, url, quality):
    format_selector = QUALITY_MAP.get(quality)
    is_audio_only = quality.startswith("Audio")
    tmp_dir = tempfile.mkdtemp()
    out_template = os.path.join(tmp_dir, f"{uuid.uuid4().hex}.%(ext)s")

    ydl_opts = {
        "quiet": True,
        "noplaylist": True,
        "format": format_selector,
        "outtmpl": out_template,
        "merge_output_format": "mp4" if not is_audio_only else None,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "progress_hooks": [_make_progress_hook(job_id)],
    }
    if is_audio_only:
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            final_path = ydl.prepare_filename(info)
            final_path = os.path.splitext(final_path)[0] + (".mp3" if is_audio_only else ".mp4")

        if not os.path.exists(final_path):
            raise RuntimeError("Download finished but the output file was not found.")

        safe_title = re.sub(r"[^\w\-. ]", "_", info.get("title", "video"))
        download_name = f"{safe_title}.{'mp3' if is_audio_only else 'mp4'}"

        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                job.update(
                    {
                        "status": "finished",
                        "percent": 100,
                        "final_path": final_path,
                        "download_name": download_name,
                    }
                )
    except Exception as e:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                job.update({"status": "error", "error": str(e)})


@app.route("/api/download/start", methods=["POST"])
def start_download():
    data = request.get_json(force=True)
    url = (data or {}).get("url", "").strip()
    quality = (data or {}).get("quality", "720p")

    if not is_valid_youtube_url(url):
        return jsonify({"error": "That doesn't look like a valid YouTube URL."}), 400
    if quality not in QUALITY_MAP:
        return jsonify({"error": "Unknown quality option."}), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "starting",
            "percent": 0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "speed": 0,
            "eta": None,
            "error": None,
            "final_path": None,
            "download_name": None,
            "pause_event": threading.Event(),
        }

    thread = threading.Thread(target=_run_download_job, args=(job_id, url, quality), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/download/progress/<job_id>")
def download_progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown job."}), 404
        # Don't leak the Event object through JSON.
        public = {k: v for k, v in job.items() if k != "pause_event"}
        public["paused"] = job["pause_event"].is_set()
    return jsonify(public)


@app.route("/api/download/pause/<job_id>", methods=["POST"])
def pause_download(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown job."}), 404
        job["pause_event"].set()
        job["status"] = "paused"
    return jsonify({"ok": True})


@app.route("/api/download/resume/<job_id>", methods=["POST"])
def resume_download(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown job."}), 404
        job["pause_event"].clear()
        job["status"] = "downloading"
    return jsonify({"ok": True})


@app.route("/api/download/file/<job_id>")
def download_file(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None or job.get("status") != "finished":
            return jsonify({"error": "File not ready."}), 400
        final_path = job["final_path"]
        download_name = job["download_name"]

    @after_this_request
    def cleanup(response):
        try:
            os.remove(final_path)
            os.rmdir(os.path.dirname(final_path))
        except OSError:
            pass
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
        return response

    return send_file(final_path, as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)