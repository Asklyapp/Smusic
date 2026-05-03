import os
import re
import time
import threading
import io
import requests
from flask import Flask, request, Response, stream_with_context
import yt_dlp

app = Flask(__name__)

# ── Telegram config ───────────────────────────────────────────────────────
BOT_TOKEN = "8749662350:AAFaCiUaVcmc20hSLkEc3pGlf1p4NlG7wU8"
CHAT_ID = "-1003992096916"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── Upload tracking ─────────────────────────────────────────────────────────
_uploading: set = set()  # Track which songs are currently uploading
_upload_lock = threading.Lock()

# ── Server-side CDN URL cache ─────────────────────────────────────────────
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


# ── Telegram helpers ──────────────────────────────────────────────────────

def telegram_upload_buffer(file_buffer: io.BytesIO, filename: str = "audio.webm"):
    """Upload a file buffer to Telegram and return file_id."""
    file_buffer.seek(0)
    url = f"{TELEGRAM_API}/sendDocument"
    files = {"document": (filename, file_buffer)}
    data = {"chat_id": CHAT_ID}
    resp = requests.post(url, data=data, files=files)
    result = resp.json()
    if result.get("ok"):
        return result["result"]["document"]["file_id"], None
    return None, result.get("description", "Unknown error")


# ── Audio proxy + Telegram upload ─────────────────────────────────────────
CHUNK = 8 * 1024

def _proxy(stream_url: str, content_type: str):
    """Just stream, no upload."""
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
        status=yt.status_code,
        headers=resp_headers,
    )


def _proxy_and_upload(stream_url: str, content_type: str, query: str):
    """
    Stream audio to client AND buffer it for Telegram upload.
    Only uploads on full-file requests (no Range header).
    Prevents duplicate uploads of the same song.
    """
    cache_key = query.lower()
    
    # Skip upload if this song is already being uploaded
    with _upload_lock:
        if cache_key in _uploading:
            print(f"[TELEGRAM] Upload already in progress for: {query}")
            return _proxy(stream_url, content_type)
        _uploading.add(cache_key)
    
    # Only upload full files, not range/seek requests
    is_range_request = 'Range' in request.headers
    should_upload = not is_range_request
    
    if is_range_request:
        print(f"[TELEGRAM] Skipping upload — this is a range/seek request: {query}")
        with _upload_lock:
            _uploading.discard(cache_key)
        return _proxy(stream_url, content_type)

    upstream_headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; audio-proxy/1.0)',
    }

    yt = requests.get(stream_url, headers=upstream_headers,
                      stream=True, timeout=(5, None))

    resp_headers = {
        'Content-Type':              content_type,
        'Accept-Ranges':             'bytes',
        'Access-Control-Allow-Origin': '*',
        'Cache-Control':             'no-store',
    }
    for h in ('Content-Length', 'Content-Range'):
        if h in yt.headers:
            resp_headers[h] = yt.headers[h]

    # Buffer for Telegram upload
    buffer = io.BytesIO()

    def generate():
        try:
            for chunk in yt.iter_content(chunk_size=CHUNK):
                if chunk:
                    buffer.write(chunk)
                    yield chunk
        finally:
            yt.close()
            # Upload in background after stream completes
            if should_upload:
                threading.Thread(
                    target=_upload_to_telegram, 
                    args=(buffer, query, cache_key), 
                    daemon=True
                ).start()
            else:
                buffer.close()
                with _upload_lock:
                    _uploading.discard(cache_key)

    return Response(
        stream_with_context(generate()),
        status=yt.status_code,
        headers=resp_headers,
    )


def _upload_to_telegram(buffer: io.BytesIO, query: str, cache_key: str):
    """Upload buffered audio to Telegram after streaming completes."""
    try:
        size = buffer.tell()
        print(f"\n[TELEGRAM] Starting upload for: {query} ({size:,} bytes)")
        
        if size < 1024:
            print(f"[TELEGRAM] ❌ File too small ({size} bytes), skipping upload")
            return
        
        file_id, err = telegram_upload_buffer(buffer, filename=f"{query.replace(' ', '_')}.webm")
        if err:
            print(f"[TELEGRAM] ❌ Upload failed: {err}")
        else:
            print(f"[TELEGRAM] ✅ SUCCESS! file_id: {file_id}")
            print(f"[TELEGRAM] 💾 Save this to Supabase: query='{query}', file_id='{file_id}'")
    except Exception as exc:
        print(f"[TELEGRAM] ❌ Exception during upload: {exc}")
    finally:
        buffer.close()
        with _upload_lock:
            _uploading.discard(cache_key)


# ── Routes ────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return "Usage: GET /audio?q=ARTIST+SONG", 200


@app.route('/audio', methods=['GET', 'HEAD'])
def get_audio():
    query = request.args.get('q', '').strip()
    if not query:
        return "Error: missing q parameter", 400

    cache_key = query.lower()
    stream_url, ct = _cache_get(cache_key)

    if not stream_url:
        try:
            stream_url, ct = resolve(query, cache_key)
        except Exception as exc:
            return f"Error resolving stream: {exc}", 500
        if not stream_url:
            return "Error: no audio stream found", 404

    try:
        resp = _proxy_and_upload(stream_url, ct or 'audio/webm', query)
        return resp

    except requests.exceptions.RequestException as exc:
        return f"Error connecting to stream: {exc}", 502


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
