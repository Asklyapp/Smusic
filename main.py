import os
import re
import time
import threading
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

# ── Active streams ────────────────────────────────────────────────────────
# When a song is currently being downloaded, we keep it here so any other
# listener who wants the same song gets the already-downloaded bytes
# immediately plus the rest as it arrives — no second YouTube call.
#
# Structure per key:
#   {
#     'buffer': BytesIO,          # all bytes downloaded so far
#     'done':   bool,             # True once download is complete
#     'lock':   threading.Lock,   # protects buffer reads
#     'waiters': list[Event],     # one Event per extra listener
#   }
_active: dict = {}
_active_lock = threading.Lock()


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

        # Try artist - title split
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
        log(f"[SUPABASE] ❌ Error: {exc}")
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


def telegram_upload(buffer: io.BytesIO, filename: str) -> tuple[str | None, str | None]:
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


# ── yt-dlp ────────────────────────────────────────────────────────────────
YTMUSIC_AVAILABLE = False
try:
    from ytmusicapi import YTMusic
    ytm = YTMusic()
    YTMUSIC_AVAILABLE = True
except ImportError:
    ytm = None


def search_youtube(query: str) -> str | None:
    if YTMUSIC_AVAILABLE:
        results = ytm.search(query, filter="songs", limit=1) or ytm.search(query, limit=1)
        if results:
            vid = results[0].get("videoId")
            if vid:
                return f"https://music.youtube.com/watch?v={vid}"
        return None
    opts = {'quiet': True, 'skip_download': True, 'extract_flat': True,
            'extractor_args': {'youtube': {'player_client': ['web_music']}}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entries = info.get('entries', [])
        return entries[0]['url'] if entries else None


_MIME = {'webm': 'audio/webm', 'm4a': 'audio/mp4',
         'mp4': 'audio/mp4', 'ogg': 'audio/ogg', 'mp3': 'audio/mpeg'}

def get_cdn_url(video_url: str) -> tuple[str | None, str]:
    opts = {'format': 'bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
            'quiet': True, 'skip_download': True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        fmts = info.get('formats', [])
        audio = [f for f in fmts if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
        if not audio:
            audio = [f for f in fmts if f.get('acodec') != 'none']
        if not audio:
            return None, 'audio/webm'
        audio.sort(key=lambda x: x.get('abr') or x.get('tbr') or 0, reverse=True)
        best = audio[0]
        return best['url'], _MIME.get(best.get('ext', ''), 'audio/webm')


# ── Shared stream ─────────────────────────────────────────────────────────
CHUNK = 8 * 1024


def _stream_from_active(key: str, content_type: str) -> Response:
    """
    Tap into an already-running download.
    Replay everything in the buffer so far, then follow along live.
    """
    def generate():
        pos = 0
        while True:
            state = _active.get(key)
            if not state:
                break

            with state['lock']:
                data = state['buffer'].getvalue()
                available = data[pos:]
                done = state['done']

            if available:
                yield available
                pos += len(available)
            elif done:
                break
            else:
                # Wait a little for more data
                time.sleep(0.1)

    return Response(
        stream_with_context(generate()),
        status=200,
        headers={
            'Content-Type':               content_type,
            'Access-Control-Allow-Origin': '*',
            'Cache-Control':              'no-store',
        },
    )


def _download_and_serve(query: str, cdn_url: str, content_type: str) -> Response:
    """
    Download from YouTube CDN once.
    - Streams to the first listener in real time
    - Any other listener who arrives mid-download gets _stream_from_active()
    - After full download, uploads to Telegram and saves to Supabase
    """
    key = query.lower()

    state = {
        'buffer':       io.BytesIO(),
        'done':         False,
        'lock':         threading.Lock(),
        'content_type': content_type,
    }

    with _active_lock:
        _active[key] = state

    def downloader():
        ok = False
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
                with state['lock']:
                    state['buffer'].write(chunk)

            ok = True
            log(f"[DOWNLOAD] ✅ Complete — {state['buffer'].tell():,} bytes — '{query}'")

        except Exception as exc:
            log(f"[DOWNLOAD] ❌ Error: {type(exc).__name__}: {exc}")
        finally:
            with state['lock']:
                state['done'] = True

        if ok:
            _upload_and_save(state['buffer'], query, content_type)

        with _active_lock:
            _active.pop(key, None)

    threading.Thread(target=downloader, daemon=True).start()

    # First listener: stream directly from the buffer as it fills
    return _stream_from_active(key, content_type)


def _upload_and_save(buffer: io.BytesIO, query: str, content_type: str):
    try:
        size = buffer.tell()
        log(f"[TELEGRAM] Uploading '{query}' ({size:,} bytes)")

        if size < 1024:
            log(f"[TELEGRAM] ❌ Too small, skipping")
            return
        if size > TELEGRAM_MAX_BYTES:
            log(f"[TELEGRAM] ❌ Too large (> 50 MB), skipping")
            return

        ext = ('webm' if 'webm' in content_type else
               'm4a'  if 'mp4'  in content_type else
               'mp3'  if 'mpeg' in content_type else 'webm')
        filename = f"{query.replace(' ', '_')}.{ext}"

        file_id, err = telegram_upload(buffer, filename)
        if err:
            log(f"[TELEGRAM] ❌ Failed: {err}")
        else:
            log(f"[TELEGRAM] ✅ Uploaded! file_id={file_id}")
            supabase_save(query, file_id, content_type)

    except Exception as exc:
        log(f"[TELEGRAM] ❌ Exception: {exc}")
    finally:
        buffer.close()


# ── Route ─────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return "Usage: GET /audio?q=Artist+-+Song", 200


@app.route('/audio', methods=['GET', 'HEAD'])
def get_audio():
    query = request.args.get('q', '').strip()
    if not query:
        return "Error: missing ?q=", 400

    key = query.lower()

    # ── 1. Check Supabase — redirect to Telegram if we have it ───────────
    row = supabase_lookup(query)
    if row:
        tg_url = telegram_get_url(row["file_id"])
        if tg_url:
            log(f"[AUDIO] Serving from Telegram: {query}")
            return redirect(tg_url, code=302)
        log(f"[AUDIO] Telegram URL fetch failed, falling back to YouTube")

    # ── 2. Already downloading? Tap into the existing stream ─────────────
    with _active_lock:
        state = _active.get(key)

    if state:
        log(f"[AUDIO] Tapping into active stream: {query}")
        return _stream_from_active(key, state['content_type'])

    # ── 3. Not cached, not active — scrape YouTube ────────────────────────
    log(f"[AUDIO] Scraping YouTube: {query}")
    try:
        if re.match(r'https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/.+', query):
            video_url = query
        else:
            video_url = search_youtube(query)
        if not video_url:
            return "Error: song not found on YouTube", 404

        cdn_url, ct = get_cdn_url(video_url)
        if not cdn_url:
            return "Error: no audio stream found", 404

    except Exception as exc:
        return f"Error: {exc}", 500

    return _download_and_serve(query, cdn_url, ct)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)