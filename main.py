import os
import re
import time
import threading
import requests
from flask import Flask, request, Response, stream_with_context
import yt_dlp

app = Flask(__name__)

# ── Server-side CDN URL cache ─────────────────────────────────────────────
# YouTube CDN URLs expire in ~6h. Cache for 4h so repeat plays skip yt-dlp.
_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_TTL = 4 * 3600

def _cache_get(key: str):
    with _cache_lock:
        e = _cache.get(key)
        if e and time.time() - e['ts'] < CACHE_TTL:
            return e['url'], e.get('ct', 'audio/webm')
    return None, None

def _cache_set(key: str, url: str, ct: str = 'audio/webm'):
    with _cache_lock:
        _cache[key] = {'url': url, 'ts': time.time(), 'ct': ct}

def _cache_del(key: str):
    with _cache_lock:
        _cache.pop(key, None)


# ── yt-dlp helpers ────────────────────────────────────────────────────────
YTMUSIC_AVAILABLE = False
try:
    from ytmusicapi import YTMusic
    ytm = YTMusic()
    YTMUSIC_AVAILABLE = True
except ImportError:
    ytm = None


def search_youtube_music(query: str):
    """Return the best-match YouTube URL for a search query."""
    if YTMUSIC_AVAILABLE:
        results = ytm.search(query, filter="songs", limit=1) or ytm.search(query, limit=1)
        if results:
            vid = results[0].get("videoId")
            if vid:
                return f"https://music.youtube.com/watch?v={vid}"
        return None
    # Fallback: plain yt-dlp search
    opts = {
        'quiet': True, 'skip_download': True, 'extract_flat': True,
        'extractor_args': {'youtube': {'player_client': ['web_music']}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entries = info.get('entries', [])
        return entries[0]['url'] if entries else None


_MIME = {'webm': 'audio/webm', 'm4a': 'audio/mp4',
         'mp4': 'audio/mp4', 'ogg': 'audio/ogg', 'mp3': 'audio/mpeg'}

def get_audio_stream(video_url: str):
    """Run yt-dlp and return (cdn_url, content_type)."""
    opts = {
        'format': 'bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True, 'skip_download': True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        fmts = info.get('formats', [])
        audio = [f for f in fmts if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
        if not audio:
            audio = [f for f in fmts if f.get('acodec') != 'none']
        if not audio:
            return None, None
        audio.sort(key=lambda x: x.get('abr') or x.get('tbr') or 0, reverse=True)
        best = audio[0]
        ct = _MIME.get(best.get('ext', ''), 'audio/webm')
        return best['url'], ct


def resolve(query: str, cache_key: str):
    """Resolve query → (cdn_url, content_type). Populates cache."""
    if re.match(r'https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/.+', query):
        video_url = query
    else:
        video_url = search_youtube_music(query)
        if not video_url:
            return None, None
    url, ct = get_audio_stream(video_url)
    if url:
        _cache_set(cache_key, url, ct)
    return url, ct


# ── Audio proxy ───────────────────────────────────────────────────────────
# Chunk size: 8 KB ≈ 0.5 s of 128 kbps audio.
# Small chunks mean the browser receives the first bytes quickly and can
# start decoding/playing before the rest of the song arrives.
CHUNK = 8 * 1024

def _proxy(stream_url: str, content_type: str):
    """
    Open a streaming connection to YouTube CDN, forward Range headers from
    the client, and pipe bytes back in small chunks so playback starts ASAP.

    iOS Safari requires:
      • Accept-Ranges: bytes           (so it can seek)
      • Content-Range / Content-Length (so it knows file size)
      • A real audio/* Content-Type    (not text/plain)
    All three are satisfied here.
    """
    # Forward the client's Range header so iOS can seek and so the CDN
    # returns a 206 Partial Content response immediately.
    upstream_headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; audio-proxy/1.0)',
    }
    if 'Range' in request.headers:
        upstream_headers['Range'] = request.headers['Range']

    yt = requests.get(stream_url, headers=upstream_headers,
                      stream=True, timeout=(5, None))

    resp_headers = {
        'Content-Type':              content_type,
        'Accept-Ranges':             'bytes',
        'Access-Control-Allow-Origin': '*',
        'Cache-Control':             'no-store',
    }
    # Pass through size/range headers so iOS knows how big the file is
    for h in ('Content-Length', 'Content-Range'):
        if h in yt.headers:
            resp_headers[h] = yt.headers[h]

    def generate():
        try:
            for chunk in yt.iter_content(chunk_size=CHUNK):
                if chunk:
                    yield chunk
        finally:
            yt.close()

    return Response(
        stream_with_context(generate()),
        status=yt.status_code,   # 200 or 206 Partial Content
        headers=resp_headers,
    )


@app.route('/')
def home():
    return "Usage: GET /audio?q=ARTIST+SONG", 200


@app.route('/audio', methods=['GET', 'HEAD'])
def get_audio():
    query = request.args.get('q', '').strip()
    if not query:
        return "Error: missing q parameter", 400

    cache_key = query.lower()

    # ── Fast path: CDN URL already cached ────────────────────────────────
    stream_url, ct = _cache_get(cache_key)

    # ── Slow path: run yt-dlp to find the CDN URL ─────────────────────────
    if not stream_url:
        try:
            stream_url, ct = resolve(query, cache_key)
        except Exception as exc:
            return f"Error resolving stream: {exc}", 500
        if not stream_url:
            return "Error: no audio stream found", 404

    # ── Proxy the audio, retrying once if the cached URL has expired ──────
    try:
        resp = _proxy(stream_url, ct or 'audio/webm')

        # YouTube returns 403/410 when a CDN URL has expired.
        # Clear cache, re-resolve, and retry once.
        if resp.status_code in (403, 410):
            _cache_del(cache_key)
            stream_url, ct = resolve(query, cache_key)
            if not stream_url:
                return "Error: stream expired and could not be refreshed", 503
            resp = _proxy(stream_url, ct or 'audio/webm')

        return resp

    except requests.exceptions.RequestException as exc:
        return f"Error connecting to stream: {exc}", 502


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)