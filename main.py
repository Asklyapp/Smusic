import os
import re
import time
import threading
import queue
import io
import requests
from flask import Flask, request, Response, stream_with_context, redirect
import yt_dlp

app = Flask(__name__)

# ── Supabase ──────────────────────────────────────────────────────────────
SUPABASE_URL = "https://bzlbyagjpblzgeiixyud.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJ6bGJ5YWdqcGJsemdlaWl4eXVkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzMzMDEwMTYsImV4cCI6MjA4ODg3NzAxNn0.HJp0_O2jf286nFwaQwecn0M1OIuNu9TDz_S3RBwXDZM"
SUPABASE_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=minimal",
}
SUPABASE_TABLE = f"{SUPABASE_URL}/rest/v1/songs"

# ── Telegram ──────────────────────────────────────────────────────────────
BOT_TOKEN    = "8749662350:AAFaCiUaVcmc20hSLkEc3pGlf1p4NlG7wU8"
CHAT_ID      = "-1003992096916"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_MAX_BYTES = 50 * 1024 * 1024

# ── Active downloads ──────────────────────────────────────────────────────
# If a song is already downloading, new listeners tap into the same download.
# Each active entry holds a list of queues — one per listener.
# The downloader puts every chunk into ALL queues simultaneously.
_active: dict = {}   # key → {'queues': [Queue, ...], 'lock': Lock, 'ct': str}
_active_lock = threading.Lock()

# ── Upload tracking ───────────────────────────────────────────────────────
_uploading: set = set()
_upload_lock = threading.Lock()

CHUNK = 8 * 1024
_SENTINEL = object()


def log(msg: str):
    print(msg, flush=True)


# ── Supabase ──────────────────────────────────────────────────────────────

def supabase_lookup(query: str) -> dict | None:
    try:
        q = query.strip().lower()
        resp = requests.get(
            SUPABASE_TABLE,
            headers=SUPABASE_HEADERS,
            params={"query": f"ilike.{q}", "limit": 1},
            timeout=5,
        )
        rows = resp.json()
        if isinstance(rows, list) and rows:
            log(f"[SUPABASE] ✅ Hit: {query}")
            return rows[0]

        parts = re.split(r'\s*-\s*', q, maxsplit=1)
        if len(parts) == 2:
            artist, title = parts[0].strip(), parts[1].strip()
            resp = requests.get(
                SUPABASE_TABLE,
                headers=SUPABASE_HEADERS,
                params={"artist": f"ilike.%{artist}%", "title": f"ilike.%{title}%", "limit": 1},
                timeout=5,
            )
            rows = resp.json()
            if isinstance(rows, list) and rows:
                log(f"[SUPABASE] ✅ Hit (artist+title): {query}")
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


# ── Telegram ──────────────────────────────────────────────────────────────

def telegram_get_url(file_id: str) -> str | None:
    try:
        resp = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=10)
        result = resp.json()
        if result.get("ok"):
            return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{result['result']['file_path']}"
        log(f"[TELEGRAM] ❌ getFile failed: {result.get('description')}")
        return None
    except Exception as exc:
        log(f"[TELEGRAM] ❌ getFile error: {exc}")
        return None


def telegram_upload_buffer(buffer: io.BytesIO, filename: str):
    buffer.seek(0)
    resp = requests.post(
        f"{TELEGRAM_API}/sendDocument",
        data={"chat_id": CHAT_ID},
        files={"document": (filename, buffer)},
        timeout=300,
    )
    result = resp.json()
    if result.get("ok"):
        return result["result"]["document"]["file_id"], None
    return None, result.get("description", "unknown")


def _upload_to_telegram(buffer: io.BytesIO, query: str, cache_key: str, content_type: str):
    try:
        size = buffer.tell()
        log(f"[TELEGRAM] Uploading '{query}' ({size:,} bytes)")
        if size < 1024:
            log(f"[TELEGRAM] ❌ Too small, skipping")
            return
        if size > TELEGRAM_MAX_BYTES:
            log(f"[TELEGRAM] ❌ Too large (>50MB), skipping")
            return
        ext = ('webm' if 'webm' in content_type else
               'm4a'  if 'mp4'  in content_type else
               'mp3'  if 'mpeg' in content_type else 'webm')
        file_id, err = telegram_upload_buffer(buffer, f"{query.replace(' ', '_')}.{ext}")
        if err:
            log(f"[TELEGRAM] ❌ Failed: {err}")
        else:
            log(f"[TELEGRAM] ✅ Uploaded! file_id={file_id}")
            supabase_save(query, file_id, content_type)
    except Exception as exc:
        log(f"[TELEGRAM] ❌ Exception: {exc}")
    finally:
        buffer.close()
        with _upload_lock:
            _uploading.discard(cache_key)
        log(f"[TELEGRAM] Done for '{query}'")


# ── yt-dlp ────────────────────────────────────────────────────────────────
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
        return best['url'], _MIME.get(best.get('ext', ''), 'audio/webm')


def resolve_youtube(query: str):
    if re.match(r'https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/.+', query):
        video_url = query
    else:
        video_url = search_youtube_music(query)
        if not video_url:
            return None, None
    return get_audio_stream(video_url)


# ── Streaming ─────────────────────────────────────────────────────────────

def _make_listener_queue() -> queue.Queue:
    """Create a new unbounded queue for a listener."""
    return queue.Queue()


def _proxy_and_upload(cdn_url: str, content_type: str, query: str) -> Response:
    """
    ONE CDN download. Every chunk goes into:
      - an unbounded queue per listener (streamed to client instantly)
      - a BytesIO buffer (uploaded to Telegram after full download)

    If another listener requests the same song mid-download, they get
    their own queue registered into the active entry and receive all
    future chunks. Queue is unbounded so puts NEVER block — sentinel
    always gets through even if a client disconnected.
    """
    cache_key = query.lower()

    # Register a queue for this listener in the active download entry
    my_queue = _make_listener_queue()

    with _active_lock:
        if cache_key in _active:
            # Already downloading — just add our queue to receive chunks
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

        # First listener — register and start the download
        with _upload_lock:
            already_uploading = cache_key in _uploading
            if not already_uploading:
                _uploading.add(cache_key)

        entry = {'queues': [my_queue], 'lock': threading.Lock(), 'ct': content_type}
        _active[cache_key] = entry

    buffer = io.BytesIO()

    def downloader():
        download_ok = False
        try:
            resp = requests.get(
                cdn_url,
                headers={'User-Agent': 'Mozilla/5.0 (compatible; audio-proxy/1.0)'},
                stream=True,
                timeout=(10, 120),
            )
            resp.raise_for_status()

            for chunk in resp.iter_content(chunk_size=CHUNK):
                if not chunk:
                    continue
                buffer.write(chunk)
                # Broadcast to all current listeners
                with entry['lock']:
                    for q in entry['queues']:
                        q.put(chunk)  # unbounded — never blocks

            download_ok = True
            log(f"[PROXY] ✅ Download complete — {buffer.tell():,} bytes — '{query}'")

        except Exception as exc:
            log(f"[PROXY] ❌ Download error: {type(exc).__name__}: {exc}")
        finally:
            # Signal all listeners we're done
            with entry['lock']:
                for q in entry['queues']:
                    q.put(_SENTINEL)
            with _active_lock:
                _active.pop(cache_key, None)

        if download_ok and not already_uploading:
            _upload_to_telegram(buffer, query, cache_key, content_type)
        else:
            buffer.close()
            if already_uploading:
                with _upload_lock:
                    _uploading.discard(cache_key)

    threading.Thread(target=downloader, daemon=True).start()

    def generate():
        while True:
            try:
                chunk = my_queue.get(timeout=60)
            except queue.Empty:
                log(f"[PROXY] Queue timeout: {query}")
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


# ── Route ─────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return "Usage: GET /audio?q=Artist+-+Song", 200


@app.route('/audio', methods=['GET', 'HEAD'])
def get_audio():
    query = request.args.get('q', '').strip()
    if not query:
        return "Error: missing ?q=", 400

    # ── 1. Supabase — redirect to Telegram if we have it ─────────────────
    row = supabase_lookup(query)
    if row:
        tg_url = telegram_get_url(row["file_id"])
        if tg_url:
            log(f"[AUDIO] Redirecting to Telegram: {query}")
            return redirect(tg_url, code=302)
        log(f"[AUDIO] Telegram URL failed, falling back to YouTube")

    # ── 2. Already downloading? Tap in (handled inside _proxy_and_upload) ─
    # ── 3. Scrape YouTube ─────────────────────────────────────────────────
    log(f"[AUDIO] Scraping YouTube: {query}")
    try:
        cdn_url, ct = resolve_youtube(query)
    except Exception as exc:
        return f"Error resolving stream: {exc}", 500

    if not cdn_url:
        return "Error: song not found", 404

    return _proxy_and_upload(cdn_url, ct or 'audio/webm', query)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)