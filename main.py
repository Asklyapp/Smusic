import os
import re
import subprocess
import json
from flask import Flask, request, Response

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False
    import requests as curl_requests

app = Flask(__name__)

# Try to import ytmusicapi; if not installed, fall back to ytsearch with web_music
YTMUSIC_AVAILABLE = False
try:
    from ytmusicapi import YTMusic
    ytm = YTMusic()
    YTMUSIC_AVAILABLE = True
except ImportError:
    ytm = None


def search_youtube_music(query):
    """Search YouTube Music for a query and return the top result video URL."""
    if YTMUSIC_AVAILABLE:
        results = ytm.search(query, filter="songs", limit=1)
        if not results:
            results = ytm.search(query, limit=1)
        if results:
            video_id = results[0].get("videoId")
            if video_id:
                return f"https://music.youtube.com/watch?v={video_id}"
        return None
    else:
        search_query = f"ytsearch1:{query}"
        ydl_opts = [
            "yt-dlp",
            "--quiet",
            "--skip-download",
            "--extract-flat",
            "--extractor-args", "youtube:player_client=web_music",
            "--dump-single-json",
            search_query,
        ]
        result = subprocess.run(ydl_opts, capture_output=True, text=True)
        if result.returncode != 0:
            return None
        try:
            info = json.loads(result.stdout)
            entries = info.get("entries", [])
            if entries:
                return entries[0]["url"]
        except json.JSONDecodeError:
            return None
        return None


def get_audio_stream_url(video_url):
    """Extract the best audio stream URL using yt-dlp with iOS client to bypass PO tokens."""
    # Use iOS client + combined format to avoid 403 on audio-only streams
    ydl_opts = [
        "yt-dlp",
        "--quiet",
        "--skip-download",
        "--format", "best[acodec!=none]/bestaudio/best",
        "--extractor-args", "youtube:player_client=ios;formats=missing_pot",
        "--dump-single-json",
        video_url,
    ]
    result = subprocess.run(ydl_opts, capture_output=True, text=True)
    if result.returncode != 0:
        return None, None
    try:
        info = json.loads(result.stdout)
        url = info.get("url")
        if not url:
            # Fallback to formats array
            formats = info.get("formats", [])
            audio_formats = [f for f in formats if f.get("acodec") != "none"]
            if not audio_formats:
                return None, None
            audio_formats.sort(
                key=lambda x: x.get("abr", 0) or x.get("tbr", 0) or 0,
                reverse=True,
            )
            url = audio_formats[0].get("url")
        ext = info.get("ext", "webm")
        return url, ext
    except json.JSONDecodeError:
        return None, None


@app.route("/")
def home():
    return "Usage: GET /audio?q=SONG+NAME+AND+ARTIST", 200


@app.route("/audio", methods=["GET"])
def get_audio():
    query = request.args.get("q")
    if not query:
        return "Error: Missing q parameter", 400

    try:
        if re.match(
            r"https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/.+",
            query,
        ):
            video_url = query
        else:
            video_url = search_youtube_music(query)
            if not video_url:
                return "Error: No search results found", 404

        stream_url, ext = get_audio_stream_url(video_url)
        if not stream_url:
            return "Error: No audio stream found", 404

        # Use curl_cffi with Chrome impersonation to match real browser TLS fingerprint
        range_header = request.headers.get("Range")
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Referer": "https://music.youtube.com/",
            "Origin": "https://music.youtube.com",
            "Connection": "keep-alive",
        }
        if range_header:
            headers["Range"] = range_header

        if CURL_CFFI_AVAILABLE:
            youtube_response = curl_requests.get(
                stream_url,
                headers=headers,
                stream=True,
                timeout=30,
                impersonate="chrome120",
            )
        else:
            youtube_response = curl_requests.get(
                stream_url,
                headers=headers,
                stream=True,
                timeout=30,
            )

        if youtube_response.status_code not in (200, 206):
            return (
                f"Error: Upstream returned {youtube_response.status_code}",
                502,
            )

        def generate():
            for chunk in youtube_response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk

        content_type = youtube_response.headers.get("Content-Type")
        if not content_type:
            content_type = f"audio/{ext}" if ext else "audio/webm"

        response = Response(
            generate(),
            status=youtube_response.status_code,
            content_type=content_type,
        )

        for h in (
            "Content-Range",
            "Accept-Ranges",
            "Content-Length",
            "Cache-Control",
            "ETag",
            "Last-Modified",
        ):
            val = youtube_response.headers.get(h)
            if val:
                response.headers[h] = val

        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Expose-Headers"] = (
            "Content-Range, Accept-Ranges, Content-Length"
        )
        return response

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Error: {str(e)}", 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
