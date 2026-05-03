import os
import re
import time
import threading
import queue
import io
import requests
from flask import Flask, request, Response, stream_with_context
import yt_dlp
from supabase import create_client, Client

app = Flask(__name__)

# ── Supabase ──────────────────────────────────────────────────────────────
SUPABASE_URL = "https://bzlbyagjpblzgeiixyud.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJ6bGJ5YWdqcGJsemdlaWl4eXVkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzMzMDEwMTYsImV4cCI6MjA4ODg3NzAxNn0.HJp0_O2jf286nFwaQwecn0M1OIuNu9TDz_S3RBwXDZM"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Telegram config ───────────────────────────────────────────────────────
BOT_TOKEN    = "8749662350:AAFaCiUaVcmc20hSLkEc3pGlf1p4NlG7wU8"
CHAT_ID      = "-1003992096916"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

TELEGRAM_MAX_BYTES = 50 * 1024 * 1024  # 50 MB hard cap

# ── Upload tracking ───────────────────────────────────────────────────────
_uploading: set = set()
_upload_lock = threading.Lock()

# ── In-memory CDN URL cache ───────────────────────────────────────────────
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


# ── Logging ───────────────────────────────────────────────────────────────

def log(msg: str):
    print(msg, flush=True)


# ── Supabase helpers ──────────────────────────────────────────────────────

def supabase_lookup(query: str) -> dict | None:
    """
    Check Supabase for a previously uploaded song.
    Returns the row dict (with file_id, content_type) or None.
    Tries exact query match first, then title+artist split.
    """
    try:
        q = query.strip().lower()

        # 1. Exact query match
        res = (supabase.table("songs")
               .select("*")
               .ilike("query", q)
               .limit(1)
               .execute())
        if res.data:
            log(f"[SUPABASE] ✅ Cache hit (query): {query}")
            return res.data[0]

        # 2. Try splitting "artist - title" or "title artist" styles
        #    and match on title + artist columns
        parts = re.split(r'\s*-\s*', q, maxsplit=1)
        if len(parts) == 2:
            artist, title = parts[0].strip(), parts[1].strip()
            res = (supabase.table("songs")
                   .select("*")
                   .ilike("artist", f"%{artist}%")
                   .ilike("title",  f"%{title}%")
                   .limit(1)
                   .execute())
            if res.data:
                log(f"[SUPABASE] ✅ Cache hit (title+artist): {query}")
                return res.data[0]

        log(f"[SUPABASE] Miss: {query}")
        return None
    except Exception as exc:
        log(f"[SUPABASE] ❌ Lookup error: {exc}")
        return None


def supabase_save(query: str, file_id: str, content_type: str):
    """Save a newly uploaded song to Supabase."""
    try:
        # Parse "artist - title" if possible
        parts = re.split(r'\s*-\s*', query.strip(), maxsplit=1)
        artist = parts[0].strip() if len(parts) == 2 else None
        title  = parts[1].strip() if len(parts) == 2 else query.strip()

        row = {
            "query":        query.strip().lower(),
            "title":        title,
            "artist":       artist,
            "file_id":      file_id,
            "content_type": content_type,
        }
        supabase.table("songs").upsert(row, on_conflict="query").execute()
        log(f"[SUPABASE] 💾 Saved: {query}")
    except Exception as exc:
        log(f"[SUPABASE] ❌ Save error: {exc}")


# ── Telegram helpers ──────────────────────────────────────────────────────

def telegram_get_stream_url(file_id: str) -> str | None:
    """
    Exchange a Telegram file_id for a fresh CDN download URL.
    Telegram file URLs expire, so always call this fresh.
    """
    try:
        resp = requests.get(
            f"{TELEGRAM_API}/getFile",
            params={"file_id": file_id},
            timeout=10,
        )
        result = resp.json()
        if result.get("ok"):
            file_path = result["result"]["file_path"]
            return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        log(f"[TELEGRAM] ❌ getFile failed: {result.get('description')}")
        return None
    except Exception as exc:
        log(f"[TELEGRAM] ❌ getFile exception: {exc}")
        return None


def telegram_upload_buffer(file_buffer: io.BytesIO, filename: str = "audio.webm") -> tuple[str | None, str | None]:
    """Upload a buffer to Telegram, return (file_id, error)."""
    file_buffer.seek(0)
    resp = requests.post(
        f"{TELEGRAM_API}/sendDocument",
        data={"chat_id": CHAT_ID},
        files={"document": (filename, file_buffer)},
        timeout=300,
    )
    result = resp.json()
    if result.get("ok"):
        return result["result"]["document"]["file_id"], None
    return None, result.get("description", "unknown error")


def _upload_to_telegram(buffer: io.BytesIO, query: str, cache_key: str, content_type: str):
    """Upload completed buffer to Telegram, then save metadata to Supabase."""
    try:
        size = buffer.tell()
        log(f"[TELEGRAM] ── Uploading '{query}' ({size:,} bytes) ──")

        if size < 1024:
            log(f"[TELEGRAM] ❌ Too small ({size} bytes), skipping")
            return

        if size > TELEGRAM_MAX_BYTES:
            log(f"[TELEGRAM] ❌ Too large ({size:,} bytes > 50 MB), skipping")
            return

        ext = ('webm' if 'webm' in content_type else
               'm4a'  if 'mp4'  in content_type else
               'mp3'  if 'mpeg' in content_type else 'webm')
        filename = f"{query.replace(' ', '_')}.{ext}"

        buffer.seek(0)
        file_id, err = telegram_upload_buffer(buffer, filename=filename)

        if err:
            log(f"[TELEGRAM] ❌ Upload failed: {err}")
        else:
            log(f"[TELEGRAM] ✅ Uploaded! file_id={file_id}")
            supabase_save(query, file_id, content_type)

    except requests.exceptions.Timeout:
        log(f"[TELEGRAM] ❌ Timed out uploading '{query}'")
    except Exception as exc:
        log(f"[TELEGRAM] ❌ Exception: {type(exc).__name__}: {exc}")
    finally:
        buffer.close()
        with _upload_lock:
            _uploading.discard(cache_key)
        log(f"[TELEGRAM] ── Upload done for '{query}' ──")


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


def resolve_youtube(query: str, cache_key: str):
    """Resolve query → YouTube CDN (cdn_url, content_type)."""
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


# ── Streaming helpers ─────────────────────────────────────────────────────
CHUNK = 8 * 1024
_SENTINEL = object()


def _proxy(stream_url: str, content_type: str):
    """Plain proxy — used for range/seek requests."""
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
      • streams chunks to client in real time
      • buffers everything
      • after full download, uploads to Telegram + saves to Supabase
    Queue is unbounded so puts never block and sentinel always gets through.
    """
    cache_key = query.lower()

    if 'Range' in request.headers:
        log(f"[PROXY] Range request — plain proxy: {query}")
        return _proxy(stream_url, content_type)

    with _upload_lock:
        if cache_key in _uploading:
            log(f"[PROXY] Already uploading: {query}")
            return _proxy(stream_url, content_type)
        _uploading.add(cache_key)

    chunk_queue: queue.Queue = queue.Queue()  # unbounded — puts never block
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
                chunk_queue.put(chunk)   # never blocks

            download_ok = True
            log(f"[PROXY] ✅ Full download complete — {buffer.tell():,} bytes — '{query}'")

        except Exception as exc:
            log(f"[PROXY] ❌ Download error '{query}': {type(exc).__name__}: {exc}")
        finally:
            chunk_queue.put(_SENTINEL)

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
                log(f"[PROXY] Queue timeout — ending stream: {query}")
                break
            if chunk is _SENTINEL:
                break
            yield chunk

    return Response(stream_with_context(generate()), status=200, headers=resp_headers)


# ── Routes ────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return "Usage: GET /audio?q=ARTIST+SONG  (e.g. ?q=Billie+Eilish+-+bury+a+friend)", 200


@app.route('/audio', methods=['GET', 'HEAD'])
def get_audio():
    query = request.args.get('q', '').strip()
    if not query:
        return "Error: missing q parameter", 400

    cache_key = query.lower()

    # ── 1. Check Supabase first ───────────────────────────────────────────
    row = supabase_lookup(query)
    if row:
        file_id      = row["file_id"]
        content_type = row.get("content_type", "audio/webm")
        log(f"[AUDIO] Serving from Telegram cache: {query}")

        stream_url = telegram_get_stream_url(file_id)
        if stream_url:
            return _proxy(stream_url, content_type)
        else:
            log(f"[AUDIO] Telegram URL fetch failed, falling back to YouTube: {query}")

    # ── 2. Fall back to YouTube scrape ────────────────────────────────────
    log(f"[AUDIO] Scraping YouTube for: {query}")
    stream_url, ct = _cache_get(cache_key)

    if not stream_url:
        try:
            stream_url, ct = resolve_youtube(query, cache_key)
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