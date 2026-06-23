import os
import re
import time
import queue
import threading
import io
import requests
from flask import Flask, request, Response, stream_with_context, redirect

app = Flask(__name__)

# ── Supabase REST config ──────────────────────────────────────────────────
SUPABASE_URL = "https://bzlbyagjpblzgeiixyud.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJ6bGJ5YWdqcGJsemdlaWl4eXVkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzMzMDEwMTYsImV4cCI6MjA4ODg3NzAxNn0.HJp0_O2jf286nFwaQwecn0M1OIuNu9TDz_S3RBwXDZM"
SUPABASE_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Accept":        "application/json",
}
SUPABASE_TABLE = f"{SUPABASE_URL}/rest/v1/songs"

# ── Telegram config ───────────────────────────────────────────────────────
BOT_TOKEN    = "8749662350:AAFaCiUaVcmc20hSLkEc3pGlf1p4NlG7wU8"
CHAT_ID      = "-1003992096916"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

CHUNK = 8 * 1024


def log(msg: str):
    print(msg, flush=True)


# ── In-memory URL cache ───────────────────────────────────────────────────
_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_TTL = 2 * 3600


def _cache_get(key: str):
    with _cache_lock:
        e = _cache.get(key)
        if e and time.time() - e['ts'] < CACHE_TTL:
            return e['url'], e.get('ct', 'audio/mp4')
    return None, None


def _cache_set(key: str, url: str, ct: str = 'audio/mp4'):
    with _cache_lock:
        _cache[key] = {'url': url, 'ts': time.time(), 'ct': ct}


def _cache_del(key: str):
    with _cache_lock:
        _cache.pop(key, None)


# ── Upload queue ──────────────────────────────────────────────────────────
# Completely independent from streaming.
# Items: (query, cache_key, file_bytes, content_type)
# One daemon thread processes uploads one at a time, forever.

_upload_queue: queue.Queue = queue.Queue()


def _upload_worker():
    while True:
        query, cache_key, file_bytes, content_type = _upload_queue.get()
        try:
            _do_upload(query, cache_key, file_bytes, content_type)
        except Exception as exc:
            log(f"[UPLOAD WORKER] ❌ {exc}")
        finally:
            _upload_queue.task_done()


threading.Thread(target=_upload_worker, daemon=True, name="upload-worker").start()


def _do_upload(query: str, cache_key: str, file_bytes: bytes, content_type: str):
    size = len(file_bytes)
    if size < 1024:
        log(f"[UPLOAD] ❌ Too small ({size} bytes), skipping: {query}")
        return
    if size > TELEGRAM_MAX_BYTES:
        log(f"[UPLOAD] ❌ Too large ({size:,} bytes), skipping: {query}")
        return

    ext = ('m4a'  if 'mp4'  in content_type else
           'webm' if 'webm' in content_type else
           'mp3'  if 'mpeg' in content_type else 'm4a')

    log(f"[UPLOAD] ⬆️  Uploading to Telegram: {query} ({size:,} bytes)")
    buf = io.BytesIO(file_bytes)
    file_id, err = telegram_upload_buffer(buf, filename=f"{query.replace(' ', '_')}.{ext}")

    if err:
        log(f"[UPLOAD] ❌ Telegram upload failed: {err}")
        return

    # Telegram success → save to Supabase, then clear stale memory cache
    log(f"[UPLOAD] ✅ Telegram done — saving to Supabase: {query}")
    supabase_save(query, file_id, content_type)
    _cache_del(cache_key)
    log(f"[UPLOAD] 🗑️  Memory cache cleared: {query}")


# ── In-progress resolve registry ──────────────────────────────────────────
# Tracks songs currently being resolved via yt-dlp.
# Multiple clients that request the same song share the resolved URL
# so yt-dlp runs only once per song, but each client opens its own
# direct proxy connection (original streaming behavior).

class ResolveState:
    def __init__(self):
        self.stream_url: str | None = None
        self.content_type: str = 'audio/mp4'
        self.resolved = threading.Event()   # set when URL is ready (or failed)
        self.error: str | None = None


_in_progress: dict[str, ResolveState] = {}
_in_progress_lock = threading.Lock()


# ── Supabase helpers ──────────────────────────────────────────────────────

def supabase_lookup(query: str) -> dict | None:
    try:
        q = query.strip().lower()
        resp = requests.get(
            SUPABASE_TABLE,
            headers=SUPABASE_HEADERS,
            params={"query": f"ilike.*{q}*", "limit": 1},
            timeout=5,
        )
        rows = resp.json()
        if isinstance(rows, list) and rows:
            return rows[0]

        parts = re.split(r'\s*-\s*', q, maxsplit=1)
        if len(parts) == 2:
            artist, title = parts[0].strip(), parts[1].strip()
            resp = requests.get(
                SUPABASE_TABLE,
                headers=SUPABASE_HEADERS,
                params={"artist": f"ilike.*{artist}*", "title": f"ilike.*{title}*", "limit": 1},
                timeout=5,
            )
            rows = resp.json()
            if isinstance(rows, list) and rows:
                return rows[0]
        return None
    except Exception as exc:
        log(f"[SUPABASE] ❌ Lookup error: {exc}")
        return None


def supabase_save(query: str, file_id: str, content_type: str):
    try:
        parts = re.split(r'\s*-\s*', query.strip(), maxsplit=1)
        artist = parts[0].strip() if len(parts) == 2 else None
        title  = parts[1].strip() if len(parts) == 2 else query.strip()
        row = {
            "query": query.strip().lower(),
            "title": title,
            "artist": artist,
            "file_id": file_id,
            "content_type": content_type,
        }
        headers = {**SUPABASE_HEADERS, "Prefer": "resolution=merge-duplicates"}
        resp = requests.post(SUPABASE_TABLE, headers=headers, json=row, timeout=10)
        if resp.status_code in (200, 201, 204):
            log(f"[SUPABASE] 💾 Saved: {query}")
        else:
            log(f"[SUPABASE] ❌ Save failed ({resp.status_code}): {resp.text}")
    except Exception as exc:
        log(f"[SUPABASE] ❌ Save error: {exc}")


# ── Telegram helpers ──────────────────────────────────────────────────────

def telegram_get_stream_url(file_id: str) -> str | None:
    try:
        resp = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=10)
        result = resp.json()
        if result.get("ok"):
            return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{result['result']['file_path']}"
        return None
    except Exception as exc:
        log(f"[TELEGRAM] ❌ getFile error: {exc}")
        return None


def telegram_upload_buffer(file_buffer: io.BytesIO, filename: str = "audio.m4a"):
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


# ── yt-dlp helpers ────────────────────────────────────────────────────────
YTMUSIC_AVAILABLE = False
try:
    from ytmusicapi import YTMusic
    ytm = YTMusic()
    YTMUSIC_AVAILABLE = True
except ImportError:
    ytm = None

_MIME = {
    'webm': 'audio/webm', 'm4a': 'audio/mp4',
    'mp4': 'audio/mp4', 'ogg': 'audio/ogg', 'mp3': 'audio/mpeg',
}


def search_youtube_music(query: str):
    if YTMUSIC_AVAILABLE:
        results = ytm.search(query, filter="songs", limit=1) or ytm.search(query, limit=1)
        if results:
            vid = results[0].get("videoId")
            if vid:
                return f"https://music.youtube.com/watch?v={vid}"
        return None
    import yt_dlp
    opts = {
        'quiet': True, 'skip_download': True, 'extract_flat': True,
        'extractor_args': {'youtube': {'player_client': ['web_music']}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entries = info.get('entries', [])
        return entries[0]['url'] if entries else None


def get_audio_stream(video_url: str):
    import yt_dlp
    opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
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
        ct = _MIME.get(best.get('ext', ''), 'audio/mp4')
        return best['url'], ct


def resolve_youtube(query: str):
    if re.match(r'https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/.+', query):
        video_url = query
    else:
        video_url = search_youtube_music(query)
        if not video_url:
            return None, None
    return get_audio_stream(video_url)


# ── Background resolve + upload download ──────────────────────────────────

def _resolve_and_prepare(query: str, cache_key: str, state: ResolveState):
    """
    Background thread:
    1. Runs yt-dlp to get the direct stream URL → sets state.resolved
       so any waiting streaming generators can open their direct proxy connection.
    2. Independently downloads the full file for Telegram upload.
       This is separate from what the client streams — the upload always
       completes even if all clients disconnect.
    """
    stream_url, ct = resolve_youtube(query)

    if not stream_url:
        log(f"[RESOLVE] ❌ No stream URL: '{query}'")
        state.error = "No audio stream found"
        state.resolved.set()
        with _in_progress_lock:
            _in_progress.pop(cache_key, None)
        return

    ct = ct or 'audio/mp4'
    state.stream_url = stream_url
    state.content_type = ct
    state.resolved.set()   # ← streaming generators unblock here
    log(f"[RESOLVE] ✅ Resolved: '{query}' ({ct})")

    # Download full file independently for upload queue
    buf = io.BytesIO()
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; audio-proxy/1.0)'}
        resp = requests.get(stream_url, headers=headers, stream=True, timeout=(10, None))
        for chunk in resp.iter_content(chunk_size=CHUNK):
            if chunk:
                buf.write(chunk)
        resp.close()
        file_bytes = buf.getvalue()
        log(f"[DOWNLOAD] ✅ Complete: '{query}' ({len(file_bytes):,} bytes) — queuing upload")
        _upload_queue.put((query, cache_key, file_bytes, ct))
    except Exception as exc:
        log(f"[DOWNLOAD] ❌ Failed: '{query}' — {exc}")
    finally:
        buf.close()
        with _in_progress_lock:
            _in_progress.pop(cache_key, None)


# ── Streaming generator (original direct-proxy behavior) ──────────────────

def _stream_direct(state: ResolveState):
    """
    Waits for yt-dlp resolution, then opens a direct HTTP proxy connection
    to the YouTube CDN — exactly the same as original streaming behavior.
    Each client gets its own independent connection.
    """
    state.resolved.wait()   # blocks until URL is ready (or failed)

    if state.error or not state.stream_url:
        log(f"[STREAM] ❌ Cannot stream — resolution failed")
        return

    log(f"[STREAM] 📡 Opening direct proxy connection")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; audio-proxy/1.0)'}
        resp = requests.get(state.stream_url, headers=headers, stream=True, timeout=(10, None))
        for chunk in resp.iter_content(chunk_size=CHUNK):
            if chunk:
                yield chunk
        resp.close()
    except Exception as exc:
        log(f"[STREAM] ❌ Proxy error: {exc}")


# ── Routes ────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return (
        f"SlurpMusic Server\n"
        f"Upload queue depth : {_upload_queue.qsize()}\n"
        f"Active resolves    : {len(_in_progress)}\n"
    ), 200


@app.route('/audio', methods=['GET', 'HEAD'])
def get_audio():
    query = request.args.get('q', '').strip()
    if not query:
        return "Error: missing q parameter", 400

    cache_key = query.lower()
    log(f"\n[AUDIO] ▶️  Request: '{query}'")

    # 1. Memory cache → redirect (YouTube CDN or Telegram CDN)
    cached_url, cached_ct = _cache_get(cache_key)
    if cached_url:
        log(f"[AUDIO] ✅ Memory cache hit: '{query}'")
        return redirect(cached_url, code=302)

    # 2. Check Supabase (fast ~200 ms)
    row = supabase_lookup(query)
    if row:
        tg_url = telegram_get_stream_url(row["file_id"])
        if tg_url:
            ct = row.get("content_type", "audio/mp4")
            _cache_set(cache_key, tg_url, ct)
            log(f"[AUDIO] ✅ Telegram CDN hit: '{query}'")
            return redirect(tg_url, code=302)
        log(f"[AUDIO] ⚠️  Supabase row found but Telegram URL failed — re-downloading")

    # 3. Check if already being resolved → join (shares the resolved URL,
    #    each client still opens its own direct proxy connection)
    with _in_progress_lock:
        if cache_key in _in_progress:
            state = _in_progress[cache_key]
            log(f"[AUDIO] 🔗 Joining in-progress resolve: '{query}'")
            return Response(
                stream_with_context(_stream_direct(state)),
                status=200,
                content_type='audio/mp4',
                headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store'},
            )

        # 4. New song — register ResolveState and return response IMMEDIATELY.
        #    The streaming generator blocks inside _stream_direct waiting for
        #    state.resolved, so AVPlayer gets its 200 OK right away and waits
        #    for data rather than timing out.
        state = ResolveState()
        _in_progress[cache_key] = state

    threading.Thread(
        target=_resolve_and_prepare,
        args=(query, cache_key, state),
        daemon=True,
        name=f"resolve-{cache_key[:20]}",
    ).start()

    log(f"[AUDIO] 🚀 New request: '{query}' — response sent immediately, resolving in background")
    return Response(
        stream_with_context(_stream_direct(state)),
        status=200,
        content_type='audio/mp4',
        headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store'},
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
