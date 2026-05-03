import os
import re
import sys
import time
import threading
import queue
import io
import requests
from flask import Flask, request, Response, stream_with_context
import yt_dlp

app = Flask(__name__)

# ── Telegram config ───────────────────────────────────────────────────────
BOT_TOKEN = "8749662350:AAFaCiUaVcmc20hSLkEc3pGlf1p4NlG7wU8"
CHAT_ID = "-1003992096916"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Telegram bots can send files up to 50 MB via sendDocument.
# Audio files above this need sendAudio which also has a 50 MB cap.
TELEGRAM_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

# ── Upload tracking ───────────────────────────────────────────────────────
_uploading: set = set()
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


# ── Telegram upload ───────────────────────────────────────────────────────

def log(msg: str):
    """Flushed print so output always appears immediately in logs."""
    print(msg, flush=True)


def _upload_to_telegram(buffer: io.BytesIO, query: str, cache_key: str, content_type: str):
    try:
        size = buffer.tell()
        log(f"[TELEGRAM] ── Upload starting for '{query}' ({size:,} bytes) ──")

        if size < 1024:
            log(f"[TELEGRAM] ❌ Skipping — file too small ({size} bytes)")
            return

        if size > TELEGRAM_MAX_BYTES:
            log(f"[TELEGRAM] ❌ Skipping — file too large ({size:,} bytes > 50 MB Telegram limit)")
            return

        ext = ('webm' if 'webm' in content_type else
               'm4a'  if 'mp4'  in content_type else
               'mp3'  if 'mpeg' in content_type else 'webm')
        filename = f"{query.replace(' ', '_')}.{ext}"
        log(f"[TELEGRAM] Filename: {filename}")

        buffer.seek(0)
        log(f"[TELEGRAM] Calling Telegram API…")

        url = f"{TELEGRAM_API}/sendDocument"
        resp = requests.post(
            url,
            data={"chat_id": CHAT_ID},
            files={"document": (filename, buffer)},
            timeout=300,   # 5-minute cap — large files can be slow
        )

        log(f"[TELEGRAM] HTTP {resp.status_code} received")
        result = resp.json()
        log(f"[TELEGRAM] API response: {result}")

        if result.get("ok"):
            file_id = result["result"]["document"]["file_id"]
            log(f"[TELEGRAM] ✅ Upload succeeded! file_id={file_id}")
            log(f"[TELEGRAM] 💾 Supabase → query='{query}', file_id='{file_id}'")
        else:
            log(f"[TELEGRAM] ❌ API error: {result.get('description', 'unknown')}")

    except requests.exceptions.Timeout:
        log(f"[TELEGRAM] ❌ Timed out uploading '{query}'")
    except Exception as exc:
        log(f"[TELEGRAM] ❌ Unexpected exception: {type(exc).__name__}: {exc}")
    finally:
        buffer.close()
        with _upload_lock:
            _uploading.discard(cache_key)
        log(f"[TELEGRAM] ── Upload thread done for '{query}' ──")


# ── Core proxy ────────────────────────────────────────────────────────────
CHUNK = 8 * 1024
QUEUE_MAX = 64
_SENTINEL = object()


def _proxy(stream_url: str, content_type: str):
    """Plain proxy for range/seek requests — no upload side-effect."""
    upstream_headers = {'User-Agent': 'Mozilla/5.0 (compatible; audio-proxy/1.0)'}
    if 'Range' in request.headers:
        upstream_headers['Range'] = request.headers['Range']

    yt = requests.get(stream_url, headers=upstream_headers, stream=True, timeout=(5, None))

    resp_headers = {
        'Content-Type':               content_type,
        'Accept-Ranges':              'bytes',
        'Access-Control-Allow-Origin': '*',
        'Cache-Control':              'no-store',
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

    return Response(stream_with_context(generate()), status=yt.status_code, headers=resp_headers)


def _proxy_and_upload(stream_url: str, content_type: str, query: str):
    """
    ONE CDN request:
      • streams to the client as bytes arrive (no startup delay)
      • buffers every byte
      • after the FULL download completes, uploads the buffer to Telegram
    Client disconnecting early has zero effect on the download/upload.
    """
    cache_key = query.lower()

    if 'Range' in request.headers:
        log(f"[PROXY] Range request — plain proxy for: {query}")
        return _proxy(stream_url, content_type)

    with _upload_lock:
        if cache_key in _uploading:
            log(f"[PROXY] Upload already running for: {query}")
            return _proxy(stream_url, content_type)
        _uploading.add(cache_key)

    chunk_queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
    buffer = io.BytesIO()

    def downloader():
        download_ok = False
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (compatible; audio-proxy/1.0)'}
            resp = requests.get(stream_url, headers=headers,
                                stream=True, timeout=(10, 120))
            resp.raise_for_status()

            for chunk in resp.iter_content(chunk_size=CHUNK):
                if not chunk:
                    continue
                buffer.write(chunk)
                try:
                    chunk_queue.put_nowait(chunk)
                except queue.Full:
                    pass  # client gone — keep downloading

            download_ok = True
            log(f"[PROXY] ✅ Full download complete — {buffer.tell():,} bytes — '{query}'")

        except Exception as exc:
            log(f"[PROXY] ❌ Download error for '{query}': {type(exc).__name__}: {exc}")
        finally:
            chunk_queue.put(_SENTINEL)

        # Upload runs in this same background thread right after download
        if download_ok:
            _upload_to_telegram(buffer, query, cache_key, content_type)
        else:
            buffer.close()
            with _upload_lock:
                _uploading.discard(cache_key)

    threading.Thread(target=downloader, daemon=True).start()

    resp_headers = {
        'Content-Type':               content_type,
        'Accept-Ranges':              'bytes',
        'Access-Control-Allow-Origin': '*',
        'Cache-Control':              'no-store',
    }

    def generate():
        while True:
            try:
                chunk = chunk_queue.get(timeout=60)
            except queue.Empty:
                log(f"[PROXY] Queue timeout for: {query}")
                break
            if chunk is _SENTINEL:
                break
            yield chunk

    return Response(stream_with_context(generate()), status=200, headers=resp_headers)


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
        return _proxy_and_upload(stream_url, ct or 'audio/webm', query)
    except requests.exceptions.RequestException as exc:
        return f"Error connecting to stream: {exc}", 502


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)