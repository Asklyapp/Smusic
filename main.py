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


# ── In-memory CDN URL cache ───────────────────────────────────────────────
# Stores YouTube CDN URLs (which expire). Cleared when a song is uploaded to
# Telegram so the next request routes through Supabase → Telegram instead.

_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_TTL = 2 * 3600   # 2 h — YouTube URLs expire, so keep TTL short


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
# Completely independent from streaming. Items: (query, cache_key, file_bytes, content_type)
# Processed one at a time by a single daemon thread. Songs are added here the
# moment their download finishes — no matter if every client disconnected.

_upload_queue: queue.Queue = queue.Queue()


def _upload_worker():
    """Single daemon thread — processes uploads sequentially, forever."""
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
    fname = f"{query.replace(' ', '_')}.{ext}"

    log(f"[UPLOAD] ⬆️  Uploading to Telegram: {query} ({size:,} bytes)")
    buf = io.BytesIO(file_bytes)
    file_id, err = telegram_upload_buffer(buf, filename=fname)

    if err:
        log(f"[UPLOAD] ❌ Telegram upload failed: {err}")
        return

    log(f"[UPLOAD] ✅ Telegram done — saving to Supabase: {query}")
    supabase_save(query, file_id, content_type)

    # Clear the memory cache so the next request routes through Supabase → Telegram
    # instead of the (now-stale) YouTube CDN URL.
    _cache_del(cache_key)
    log(f"[UPLOAD] 🗑️  Memory cache cleared for: {query}")


# ── In-progress download registry ────────────────────────────────────────
# Tracks songs currently being downloaded. Additional clients that request the
# same song while it's downloading join the same DownloadState instead of
# starting a new download.

class DownloadState:
    def __init__(self, content_type: str = 'audio/mp4'):
        self.chunks: list[bytes] = []
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.content_type = content_type
        self.error: str | None = None


_in_progress: dict[str, DownloadState] = {}
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
        resp = requests.get(
            f"{TELEGRAM_API}/getFile",
            params={"file_id": file_id},
            timeout=10,
        )
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
    """Returns (stream_url, content_type) or (None, None)."""
    if re.match(r'https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/.+', query):
        video_url = query
    else:
        video_url = search_youtube_music(query)
        if not video_url:
            return None, None
    return get_audio_stream(video_url)


# ── Download + broadcast ──────────────────────────────────────────────────

def _resolve_and_download(query: str, cache_key: str, state: DownloadState):
    """
    Runs in its own thread.
    Step 1: Resolve the yt-dlp stream URL (slow — 5-15 s).
    Step 2: Download the audio, appending chunks to state so streaming clients
            can read them live.
    Step 3: Queue the full file for Telegram upload — independently of whether
            any client is still connected.
    """
    # Step 1 — resolve
    stream_url, ct = resolve_youtube(query)
    if not stream_url:
        log(f"[DOWNLOAD] ❌ No stream URL found for: '{query}'")
        state.error = "No audio stream found"
        state.done.set()
        with _in_progress_lock:
            _in_progress.pop(cache_key, None)
        return

    ct = ct or 'audio/mp4'
    state.content_type = ct
    log(f"[DOWNLOAD] 🎵 Resolved '{query}' → {ct}")

    # Step 2 — download and broadcast chunks
    buf = io.BytesIO()
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; audio-proxy/1.0)'}
        resp = requests.get(stream_url, headers=headers, stream=True, timeout=(10, None))
        for chunk in resp.iter_content(chunk_size=CHUNK):
            if chunk:
                with state.lock:
                    state.chunks.append(chunk)
                buf.write(chunk)
        resp.close()

        file_bytes = buf.getvalue()
        log(f"[DOWNLOAD] ✅ Complete: '{query}' ({len(file_bytes):,} bytes) — queuing upload")

        # Step 3 — queue upload (independent; runs even if clients disconnected)
        _upload_queue.put((query, cache_key, file_bytes, ct))

    except Exception as exc:
        log(f"[DOWNLOAD] ❌ Error: '{query}' — {exc}")
        state.error = str(exc)
    finally:
        buf.close()
        state.done.set()
        with _in_progress_lock:
            _in_progress.pop(cache_key, None)


def _stream_from_state(state: DownloadState):
    """
    Generator that yields chunks from a DownloadState as they arrive.
    Multiple clients can call this simultaneously with their own idx —
    they all read from the same shared chunk list without interfering.
    Waits for chunks while the download is in progress.
    """
    idx = 0
    while True:
        with state.lock:
            available = state.chunks[idx:]

        for chunk in available:
            yield chunk
        idx += len(available)

        if state.done.is_set():
            # Drain any chunks written right before done was set
            with state.lock:
                final = state.chunks[idx:]
            for chunk in final:
                yield chunk
            break

        time.sleep(0.02)


# ── Routes ────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return (
        f"SlurpMusic Server\n"
        f"Upload queue depth : {_upload_queue.qsize()}\n"
        f"Active downloads   : {len(_in_progress)}\n"
    ), 200


@app.route('/audio', methods=['GET', 'HEAD'])
def get_audio():
    query = request.args.get('q', '').strip()
    if not query:
        return "Error: missing q parameter", 400

    cache_key = query.lower()
    log(f"\n[AUDIO] ▶️  Request: '{query}'")

    # 1. Check in-memory URL cache (fastest path — YouTube CDN redirect)
    cached_url, cached_ct = _cache_get(cache_key)
    if cached_url:
        log(f"[AUDIO] ✅ Memory cache hit: '{query}'")
        return redirect(cached_url, code=302)

    # 2. Check if song is already being downloaded → join that download
    with _in_progress_lock:
        if cache_key in _in_progress:
            state = _in_progress[cache_key]
            log(f"[AUDIO] 🔗 Joining in-progress download: '{query}'")
            return Response(
                stream_with_context(_stream_from_state(state)),
                status=200,
                content_type=state.content_type,
                headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store'},
            )

    # 3. Check Supabase (fast ~200 ms) — already uploaded to Telegram?
    row = supabase_lookup(query)
    if row:
        tg_url = telegram_get_stream_url(row["file_id"])
        if tg_url:
            ct = row.get("content_type", "audio/mp4")
            _cache_set(cache_key, tg_url, ct)
            log(f"[AUDIO] ✅ Telegram CDN hit: '{query}'")
            return redirect(tg_url, code=302)
        log(f"[AUDIO] ⚠️  Supabase row found but Telegram URL failed — re-downloading")

    # 4. Not cached anywhere. Register a DownloadState NOW and return the
    #    streaming response IMMEDIATELY so AVPlayer doesn't time out waiting
    #    for yt-dlp to resolve (which can take 5-15 seconds).
    state = DownloadState(content_type='audio/mp4')
    with _in_progress_lock:
        # Double-check — another thread might have started a download between
        # the check above and acquiring the lock now.
        if cache_key in _in_progress:
            state = _in_progress[cache_key]
            log(f"[AUDIO] 🔗 Joining in-progress download (late): '{query}'")
        else:
            _in_progress[cache_key] = state
            # Start resolve + download entirely in the background.
            # The streaming response below will wait for chunks as they arrive.
            threading.Thread(
                target=_resolve_and_download,
                args=(query, cache_key, state),
                daemon=True,
                name=f"dl-{cache_key[:20]}",
            ).start()
            log(f"[AUDIO] 🚀 New stream started: '{query}' (response sent immediately)")

    return Response(
        stream_with_context(_stream_from_state(state)),
        status=200,
        content_type='audio/mp4',
        headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store'},
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
