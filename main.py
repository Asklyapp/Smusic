import os
import re
import time
import threading
import queue
import io
import requests
import uuid
from flask import Flask, request, Response, stream_with_context, redirect, jsonify
import yt_dlp

app = Flask(__name__)

# ── Supabase REST config ──────────────────────────────────────────────────
SUPABASE_URL = "https://bzlbyagjpblzgeiixyud.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJ6bGJ5YWdqcGJsemdlaWl4eXVkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3MzMzMDEwMTYsImV4cCI6MjA4ODg3NzAxNn0.HJp0_O2jf286nFwaQwecn0M1OIuNu9TDz_S3RBwXDZM"
SUPABASE_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=minimal",
}
SUPABASE_TABLE = f"{SUPABASE_URL}/rest/v1/songs"

# ── Telegram config ───────────────────────────────────────────────────────
BOT_TOKEN    = "8749662350:AAFaCiUaVcmc20hSLkEc3pGlf1p4NlG7wU8"
CHAT_ID      = "-1003992096916"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

TELEGRAM_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

# ── Upload tracking ───────────────────────────────────────────────────────
_uploading: set = set()
_upload_lock = threading.Lock()

# ── Active downloads ──────────────────────────────────────────────────────
# If a song is already downloading, new listeners get their own queue
# and receive every chunk — no second YouTube call ever.
_active: dict = {}
_active_lock = threading.Lock()

# ── In-memory CDN URL cache ───────────────────────────────────────────────
_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_TTL = 4 * 3600

# ── Progressive streaming sessions ───────────────────────────────────────
# NEW: Stores partial buffers for instant streaming while downloading
_sessions: dict = {}
_sessions_lock = threading.Lock()
SESSION_CLEANUP_INTERVAL = 300  # 5 minutes


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


# ── Supabase REST helpers ─────────────────────────────────────────────────

def supabase_lookup(query: str) -> dict | None:
    try:
        q = query.strip().lower()

        # 1. Exact query match
        resp = requests.get(
            SUPABASE_TABLE,
            headers=SUPABASE_HEADERS,
            params={"query": f"ilike.{q}", "limit": 1},
            timeout=5,
        )
        rows = resp.json()
        if isinstance(rows, list) and rows:
            log(f"[SUPABASE] ✅ Cache hit (query): {query}")
            return rows[0]

        # 2. Artist + title split
        parts = re.split(r'\s*-\s*', q, maxsplit=1)
        if len(parts) == 2:
            artist, title = parts[0].strip(), parts[1].strip()
            resp = requests.get(
                SUPABASE_TABLE,
                headers=SUPABASE_HEADERS,
                params={
                    "artist": f"ilike.%{artist}%",
                    "title":  f"ilike.%{title}%",
                    "limit":  1,
                },
                timeout=5,
            )
            rows = resp.json()
            if isinstance(rows, list) and rows:
                log(f"[SUPABASE] ✅ Cache hit (artist+title): {query}")
                return rows[0]

        log(f"[SUPABASE] Miss: {query}")
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
            "query":        query.strip().lower(),
            "title":        title,
            "artist":       artist,
            "file_id":      file_id,
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
    """Exchange a Telegram file_id for a fresh temporary CDN URL."""
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


def telegram_upload_buffer(file_buffer: io.BytesIO, filename: str = "audio.webm"):
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


# ── Progressive Streaming Session Management ─────────────────────────────
# NEW ARCHITECTURE: Instead of proxying the whole file through Python,
# we create a session that the client can stream from directly while we
# download/upload in the background.

CHUNK = 8 * 1024
_SENTINEL = object()


def _cleanup_old_sessions():
    """Remove sessions older than SESSION_CLEANUP_INTERVAL."""
    now = time.time()
    with _sessions_lock:
        expired = [sid for sid, s in _sessions.items() 
                   if now - s.get('created', 0) > SESSION_CLEANUP_INTERVAL and s.get('complete')]
        for sid in expired:
            if _sessions[sid].get('buffer'):
                _sessions[sid]['buffer'].close()
            del _sessions[sid]
            log(f"[SESSION] Cleaned up expired session {sid}")


def _create_session(query: str, content_type: str) -> str:
    """Create a new streaming session and return its ID."""
    session_id = str(uuid.uuid4())[:12]
    with _sessions_lock:
        _sessions[session_id] = {
            'query': query,
            'content_type': content_type,
            'buffer': io.BytesIO(),
            'chunks': [],  # List of (offset, size) for range requests
            'total_size': 0,
            'complete': False,
            'failed': False,
            'error': None,
            'created': time.time(),
            'last_access': time.time(),
            'condition': threading.Condition(),
            'uploaded': False,
        }
    return session_id


def _get_session(session_id: str):
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session:
            session['last_access'] = time.time()
        return session


def _session_stream(session_id: str, range_header: str = None):
    """
    Stream from a session. Supports range requests.
    This is what the client hits when streaming from /stream/<session_id>
    """
    session = _get_session(session_id)
    if not session:
        return "Session not found", 404

    if session.get('failed'):
        return f"Download failed: {session.get('error')}", 500

    ct = session['content_type']

    # Parse range header
    start = 0
    end = None
    if range_header:
        match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if match:
            start = int(match.group(1))
            if match.group(2):
                end = int(match.group(2))

    def generate():
        buffer = session['buffer']
        condition = session['condition']
        last_pos = start

        while True:
            with condition:
                # Wait until we have data or download is complete/failed
                while last_pos >= session['total_size'] and not session.get('complete') and not session.get('failed'):
                    condition.wait(timeout=1.0)

                if session.get('failed'):
                    break

                # Read available data
                buffer.seek(last_pos)
                available = session['total_size'] - last_pos

                if available > 0:
                    to_read = min(available, CHUNK)
                    if end is not None:
                        to_read = min(to_read, end - last_pos + 1)

                    if to_read <= 0:
                        break

                    data = buffer.read(to_read)
                    last_pos += len(data)
                    yield data

                    if end is not None and last_pos > end:
                        break
                elif session.get('complete'):
                    break
                else:
                    # No data yet, loop around and wait
                    pass

    # Determine headers
    headers = {
        'Content-Type': ct,
        'Accept-Ranges': 'bytes',
        'Access-Control-Allow-Origin': '*',
        'Cache-Control': 'no-store',
    }

    if range_header and session.get('complete'):
        # We know total size, can serve proper range response
        total = session['total_size']
        end_val = end if end is not None else total - 1
        headers['Content-Length'] = str(end_val - start + 1)
        headers['Content-Range'] = f'bytes {start}-{end_val}/{total}'
        status = 206
    elif range_header and not session.get('complete'):
        # Don't know total size yet, stream what we have
        status = 206
    else:
        status = 200

    return Response(stream_with_context(generate()), status=status, headers=headers)


def _download_to_session(session_id: str, stream_url: str, query: str):
    """
    Background downloader: fetches from YouTube into the session buffer,
    then uploads to Telegram when complete.
    """
    session = _get_session(session_id)
    if not session:
        return

    buffer = session['buffer']
    condition = session['condition']
    cache_key = query.lower()

    download_ok = False

    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; audio-proxy/1.0)'}
        resp = requests.get(stream_url, headers=headers, stream=True, timeout=(10, 120))
        resp.raise_for_status()

        for chunk in resp.iter_content(chunk_size=CHUNK):
            if not chunk:
                continue

            with condition:
                pos = buffer.tell()
                buffer.write(chunk)
                session['total_size'] = buffer.tell()
                session['chunks'].append((pos, len(chunk)))
                condition.notify_all()  # Wake up all waiting streamers

        download_ok = True
        log(f"[SESSION] ✅ Download complete — {buffer.tell():,} bytes — '{query}'")

    except Exception as exc:
        log(f"[SESSION] ❌ Download error '{query}': {type(exc).__name__}: {exc}")
        with condition:
            session['failed'] = True
            session['error'] = str(exc)
            condition.notify_all()
    finally:
        with condition:
            session['complete'] = True
            condition.notify_all()

    # Upload to Telegram if download succeeded
    if download_ok:
        with _upload_lock:
            if cache_key not in _uploading:
                _uploading.add(cache_key)
                # Create a new buffer for upload (copy)
                upload_buffer = io.BytesIO()
                buffer.seek(0)
                upload_buffer.write(buffer.read())
                threading.Thread(
                    target=_upload_to_telegram,
                    args=(upload_buffer, query, cache_key, session['content_type']),
                    daemon=True
                ).start()
            else:
                log(f"[TELEGRAM] Already uploading '{query}', skipping duplicate")


# ── Legacy proxy (kept for fallback) ──────────────────────────────────────

def _proxy(stream_url: str, content_type: str):
    """Legacy: Proxy a stream through the server."""
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
    LEGACY fallback: One YouTube CDN request with tap-in support.
    Only used if progressive streaming is disabled or fails.
    """
    cache_key = query.lower()
    my_queue: queue.Queue = queue.Queue()

    with _active_lock:
        if cache_key in _active:
            log(f"[PROXY] Tapping into active download: {query}")
            _active[cache_key]['queues'].append(my_queue)
            ct = _active[cache_key]['ct']

            def generate_tap():
                while True:
                    try:
                        chunk = my_queue.get(timeout=60)
                    except queue.Empty:
                        break
                    if chunk is _SENTINEL:
                        break
                    yield chunk

            return Response(
                stream_with_context(generate_tap()),
                status=200,
                headers={
                    'Content-Type':               ct,
                    'Accept-Ranges':              'bytes',
                    'Access-Control-Allow-Origin': '*',
                    'Cache-Control':              'no-store',
                },
            )

        _active[cache_key] = {
            'queues': [my_queue],
            'lock':   threading.Lock(),
            'ct':     content_type,
        }

    with _upload_lock:
        _uploading.add(cache_key)

    buffer = io.BytesIO()
    entry  = _active[cache_key]

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
                with entry['lock']:
                    for q in entry['queues']:
                        q.put(chunk)

            download_ok = True
            log(f"[PROXY] ✅ Full download complete — {buffer.tell():,} bytes — '{query}'")

        except Exception as exc:
            log(f"[PROXY] ❌ Download error '{query}': {type(exc).__name__}: {exc}")
        finally:
            with entry['lock']:
                for q in entry['queues']:
                    q.put(_SENTINEL)
            with _active_lock:
                _active.pop(cache_key, None)

        if download_ok:
            _upload_to_telegram(buffer, query, cache_key, content_type)
        else:
            buffer.close()
            with _upload_lock:
                _uploading.discard(cache_key)

    threading.Thread(target=downloader, daemon=True).start()

    def generate():
        while True:
            try:
                chunk = my_queue.get(timeout=60)
            except queue.Empty:
                log(f"[PROXY] Queue timeout — ending stream: {query}")
                break
            if chunk is _SENTINEL:
                break
            yield chunk

    return Response(
        stream_with_context(generate()),
        status=200,
        headers={
            'Content-Type':               content_type,
            'Accept-Ranges':              'bytes',
            'Access-Control-Allow-Origin': '*',
            'Cache-Control':              'no-store',
        },
    )


# ── Routes ────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return """Usage:
GET /audio?q=Artist+-+Song+Title  → Start progressive streaming (returns session JSON)
GET /stream/<session_id>          → Stream audio from session (use this URL in your app)
GET /audio/legacy?q=...           → Old direct proxy mode
""", 200


@app.route('/audio', methods=['GET', 'HEAD'])
def get_audio():
    """
    NEW: Returns a session URL immediately. The client can start streaming
    from /stream/<session_id> while we download in the background.
    """
    query = request.args.get('q', '').strip()
    if not query:
        return "Error: missing q parameter", 400

    cache_key = query.lower()

    # ── 1. Check Supabase first ────────────────────────────────────────
    row = supabase_lookup(query)
    if row:
        tg_url = telegram_get_stream_url(row["file_id"])
        if tg_url:
            log(f"[AUDIO] Redirecting to Telegram CDN: {query}")
            return redirect(tg_url, code=302)
        log(f"[AUDIO] Telegram URL failed, falling back to YouTube")

    # ── 2. Check if already downloading ────────────────────────────────
    with _active_lock:
        if cache_key in _active:
            # Someone else is already downloading via legacy, tap in
            log(f"[AUDIO] Active legacy download found for: {query}")
            # Fall through to legacy mode for simplicity
            pass

    # ── 3. Check if already uploaded/uploading ───────────────────────
    with _upload_lock:
        if cache_key in _uploading:
            log(f"[AUDIO] Already uploading '{query}', will stream via legacy")
            # Fall through to legacy
            pass

    # ── 4. Resolve YouTube URL ──────────────────────────────────────
    stream_url, ct = _cache_get(cache_key)
    if not stream_url:
        log(f"[AUDIO] Scraping YouTube for: {query}")
        try:
            stream_url, ct = resolve_youtube(query, cache_key)
        except Exception as exc:
            return f"Error resolving stream: {exc}", 500
        if not stream_url:
            return "Error: no audio stream found", 404

    # ── 5. Create progressive streaming session ─────────────────────
    # This is the NEW behavior: we return a session URL immediately,
    # and the client streams from there while we download in background.
    session_id = _create_session(query, ct or 'audio/webm')

    # Start background download immediately
    threading.Thread(
        target=_download_to_session,
        args=(session_id, stream_url, query),
        daemon=True
    ).start()

    # Build the stream URL
    host = request.host_url.rstrip('/')
    stream_url = f"{host}/stream/{session_id}"

    log(f"[AUDIO] ✅ Created session {session_id} for '{query}'")

    # Return JSON with the stream URL (your app uses this to start playback)
    return jsonify({
        "query": query,
        "session_id": session_id,
        "stream_url": stream_url,
        "content_type": ct or 'audio/webm',
        "status": "buffering",
        "message": "Stream ready. Start playback immediately using stream_url."
    })


@app.route('/stream/<session_id>', methods=['GET', 'HEAD'])
def stream_session(session_id):
    """
    NEW: Stream from an active session. Supports range requests for seeking.
    This is what your app/player hits to actually get the audio data.
    """
    range_header = request.headers.get('Range')
    return _session_stream(session_id, range_header)


@app.route('/audio/legacy', methods=['GET', 'HEAD'])
def get_audio_legacy():
    """
    OLD behavior: Direct proxy through Python. Use if progressive streaming
    has issues. Uploads to Telegram after complete download.
    """
    query = request.args.get('q', '').strip()
    if not query:
        return "Error: missing q parameter", 400

    cache_key = query.lower()

    # ── 1. Supabase ──────────────────────────────────────────────────
    row = supabase_lookup(query)
    if row:
        tg_url = telegram_get_stream_url(row["file_id"])
        if tg_url:
            log(f"[AUDIO/LEGACY] Redirecting to Telegram: {query}")
            return redirect(tg_url, code=302)

    # ── 2. Resolve YouTube ────────────────────────────────────────────
    stream_url, ct = _cache_get(cache_key)
    if not stream_url:
        log(f"[AUDIO/LEGACY] Scraping YouTube for: {query}")
        try:
            stream_url, ct = resolve_youtube(query, cache_key)
        except Exception as exc:
            return f"Error resolving stream: {exc}", 500
        if not stream_url:
            return "Error: no audio stream found", 404

    # ── 3. Proxy and upload ─────────────────────────────────────────
    # FIXED: Removed the broken Range header check that was preventing uploads!
    return _proxy_and_upload(stream_url, ct or 'audio/webm', query)


@app.route('/session/<session_id>/status')
def session_status(session_id):
    """Check download progress of a session."""
    session = _get_session(session_id)
    if not session:
        return {"error": "Session not found"}, 404

    return jsonify({
        "session_id": session_id,
        "query": session['query'],
        "total_size": session['total_size'],
        "complete": session.get('complete', False),
        "failed": session.get('failed', False),
        "uploaded": session.get('uploaded', False),
    })


# ── Cleanup thread ────────────────────────────────────────────────────────

def _cleanup_worker():
    while True:
        time.sleep(SESSION_CLEANUP_INTERVAL)
        _cleanup_old_sessions()

threading.Thread(target=_cleanup_worker, daemon=True).start()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
