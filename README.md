# YouTube Downloader (minimal)

A small local web app: paste a YouTube link, pick a quality (1080p / 720p /
480p / 360p / audio-only), click Download.

## Why this needs a backend

Browsers can't pull video data directly from YouTube (no CORS access, and
YouTube's stream URLs are signed/obfuscated). So this ships as a tiny Flask
server that uses `yt-dlp` to fetch the stream and hand you back a file. It
runs entirely on your own machine — nothing is uploaded anywhere.

## Setup

1. Install [ffmpeg](https://ffmpeg.org/download.html) and make sure it's on
   your PATH (needed to merge video+audio for anything above 360p, and for
   the audio-only MP3 option).
   - macOS: `brew install ffmpeg`
   - Windows: `winget install ffmpeg` (or download a build and add it to PATH)
   - Linux: `sudo apt install ffmpeg`

2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the app:
   ```bash
   python app.py
   ```

4. Open **http://127.0.0.1:5000** in your browser.

## Notes

- Only use this on videos you have the right to download (your own content,
  public domain, Creative Commons, or with the rights holder's permission).
  Downloading copyrighted videos without permission can violate YouTube's
  Terms of Service.
- If a specific resolution isn't available for a given video, yt-dlp
  automatically falls back to the closest one it can find.
- This is a minimal/dev setup (Flask's built-in server) — fine for personal
  local use, not meant to be deployed publicly as-is.
