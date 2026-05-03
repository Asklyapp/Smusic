import os
import re
import time
import threading
from flask import Flask, request, Response, redirect
import yt_dlp

app = Flask(__name__)

# ── Server-side URL cache ─────────────────────────────────────────────────
# YouTube CDN URLs expire after ~6 hours. We cache for 4 hours to be safe.
# On Android the 2-3s delay is yt-dlp processing time; with caching,
# repeated plays of the same song are instant on both platforms.
_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_TTL = 4 * 3600  # 4 hours in seconds

def _cache_get(key: str):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry['ts'] < CACHE_TTL:
            return entry['url']
    return None

def _cache_set(key: str, url: str):
    with _cache_lock:
        _cache[key] = {'url': url, 'ts': time.time()}

# ── YouTube Music / yt-dlp helpers ────────────────────────────────────────
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
        # Fallback: use ytsearch with web_music client for music-biased results
        search_query = f"ytsearch1:{query}"
        ydl_opts = {
            'quiet': True,
            'skip_download': True,
            'extract_flat': True,
            'extractor_args': {
                'youtube': {
                    'player_client': ['web_music'],
                },
            },
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            entries = info.get('entries', [])
            if not entries:
                return None
            return entries[0]['url']


def get_audio_stream_url(video_url):
    """Extract the best audio-only stream URL from a YouTube video.

    Prefers opus/m4a audio-only formats. Falls back to any audio-bearing
    format. Returns None if nothing is found.
    """
    ydl_opts = {
        # Prefer audio-only; opus at 160 kbps is YouTube's best audio-only
        'format': 'bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'skip_download': True,
        'extract_flat': False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        formats = info.get('formats', [])

        # Audio-only streams first (no video track)
        audio_formats = [
            f for f in formats
            if f.get('acodec') != 'none' and f.get('vcodec') == 'none'
        ]
        if not audio_formats:
            audio_formats = [f for f in formats if f.get('acodec') != 'none']
        if not audio_formats:
            return None, None

        audio_formats.sort(
            key=lambda x: x.get('abr', 0) or x.get('tbr', 0) or 0,
            reverse=True,
        )
        best = audio_formats[0]
        # Return URL and content-type so we can pass it through if needed
        mime = best.get('ext', 'webm')
        mime_map = {'webm': 'audio/webm', 'm4a': 'audio/mp4',
                    'mp4': 'audio/mp4', 'ogg': 'audio/ogg', 'mp3': 'audio/mpeg'}
        content_type = mime_map.get(mime, 'audio/webm')
        return best['url'], content_type


# ── Routes ────────────────────────────────────────────────────────────────
@app.route('/')
def home():
    return "Usage: GET /audio?q=SONG+NAME+AND+ARTIST", 200


@app.route('/audio', methods=['GET', 'HEAD'])
def get_audio():
    query = request.args.get('q')
    if not query:
        return "Error: Missing q parameter", 400

    # Normalise cache key — strip the JS cache-buster (_cb param) if present
    cache_key = query.strip().lower()

    # ── Cache hit: instant response ───────────────────────────────────────
    cached_url = _cache_get(cache_key)
    if cached_url:
        # 302 redirect → browser loads directly from YouTube CDN.
        # This gives iOS Safari the proper Content-Type, Accept-Ranges, and
        # Content-Length headers it needs to start buffering immediately.
        resp = redirect(cached_url, 302)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    # ── Cache miss: resolve via yt-dlp ───────────────────────────────────
    try:
        if re.match(r'https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/.+', query):
            video_url = query
        else:
            video_url = search_youtube_music(query)
            if not video_url:
                return "Error: No search results found", 404

        stream_url, _ = get_audio_stream_url(video_url)
        if not stream_url:
            return "Error: No audio stream found", 404

        # Store in cache for subsequent plays
        _cache_set(cache_key, stream_url)

        # 302 redirect so the browser talks directly to YouTube CDN.
        # iOS Safari REQUIRES Accept-Ranges + Content-Length (which YouTube
        # CDN provides) to start playback without a 25-30 second stall.
        # Returning text/plain (the old behaviour) makes iOS retry dozens of
        # times before giving up — hence the 30-second delay.
        resp = redirect(stream_url, 302)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    except Exception as e:
        return f"Error: {str(e)}", 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)