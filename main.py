import os
import re
import time
import threading
import io
import requests
from flask import Flask, request, Response, stream_with_context, redirect
import yt_dlp

app = Flask(__name__)

# ── Supabase REST config ──────────────────────────────────────────────────
SUPABASE_URL = "https://bzlbyagjpblzgeiixyud.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJ6bGJ5YWdqcGJsemdlaWl4eXVkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3MzMzMDEwMTYsImV4cCI6MjA4ODg3NzAxNn0.HJp0_O2jf286nFwaQwecn0M1OIuNu9TDz_S3RBwXDZM"

# FIX: Only send apikey header. DO NOT send Authorization header with anon key.
# When both are present, Kong prioritizes Authorization and rejects it as invalid JWT.
SUPABASE_HEADERS = {
    "apikey":       SUPABASE_KEY,
    "Content-Type": "application/json",
    "Accept":       "application/vnd.pgrst.object+json, application/json",
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


# ── Supabase REST helpers ─────────────────────────────────────────────────

def supabase_lookup(query: str) -> dict | None:
    try:
        q = query.strip().lower()
        log(f"[SUPABASE] Looking up: '{q}'")

        # 1. Exact query match
        url = f"{SUPABASE_TABLE}?query=ilike.{q}&limit=1"
        log(f"[SUPABASE] URL: {url}")

        resp = requests.get(url, headers=SUPABASE_HEADERS, timeout=5)
        log(f"[SUPABASE] Lookup status: {resp.status_code}")

        if resp.status_code == 401:
            log(f"[SUPABASE] ❌ 401 UNAUTHORIZED")
            log(f"[SUPABASE] Response body: {resp.text}")
            return None
        if resp.status_code != 200:
            log(f"[SUPABASE] ❌ Unexpected status: {resp.status_code}")
            log(f"[SUPABASE] Response: {resp.text}")
            return None

        rows = resp.json()
        log(f"[SUPABASE] Lookup rows: {rows}")

        if isinstance(rows, list) and rows:
            log(f"[SUPABASE] ✅ Cache hit (query): {query}")
            return rows[0]

        # 2. Artist + title split
        parts = re.split(r'\s*-\s*', q, maxsplit=1)
        if len(parts) == 2:
            artist, title = parts[0].strip(), parts[1].strip()
            url = f"{SUPABASE_TABLE}?artist=ilike.%{artist}%&title=ilike.%{title}%&limit=1"
            log(f"[SUPABASE] Fallback URL: {url}")

            resp = requests.get(url, headers=SUPABASE_HEADERS, timeout=5)
            log(f"[SUPABASE] Fallback status: {resp.status_code}")

            if resp.status_code == 401:
                log(f"[SUPABASE] ❌ 401 UNAUTHORIZED on fallback")
                return None
            if resp.status_code != 200:
                log(f"[SUPABASE] ❌ Unexpected status on fallback: {resp.status_code}")
                return None

            rows = resp.json()
            log(f"[SUPABASE] Fallback rows: {rows}")

            if isinstance(rows, list) and rows:
                log(f"[SUPABASE] ✅ Cache hit (artist+title): {query}")
                return rows[0]

        log(f"[SUPABASE] Miss: {query}")
        return None

    except Exception as exc:
        log(f"[SUPABASE] ❌ Lookup error: {exc}")
        import traceback
        log(f"[SUPABASE] Traceback: {traceback.format_exc()}")
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

        headers = {
            **SUPABASE_HEADERS,
            "Prefer": "resolution=merge-duplicates",
        }

        log(f"[SUPABASE] Saving row: {row}")
        log(f"[SUPABASE] Save URL: {SUPABASE_TABLE}")

        resp = requests.post(SUPABASE_TABLE, headers=headers, json=row, timeout=10)

        log(f"[SUPABASE] Save status: {resp.status_code}")
        log(f"[SUPABASE] Save response: {resp.text}")

        if resp.status_code in (200, 201, 204):
            log(f"[SUPABASE] 💾 Saved: {query}")
        elif resp.status_code == 401:
            log(f"[SUPABASE] ❌ 401 UNAUTHORIZED — Check API key or RLS")
        else:
            log(f"[SUPABASE] ❌ Save failed ({resp.status_code}): {resp.text}")

    except Exception as exc:
        log(f"[SUPABASE] ❌ Save error: {exc}")
        import traceback
        log(f"[SUPABASE] Traceback: {traceback.format_exc()}")


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
            buffer.close()
            with _upload_lock:
                _uploading.discard(cache_key)
            return

        if size > TELEGRAM_MAX_BYTES:
            log(f"[TELEGRAM] ❌ Too large ({size:,} bytes > 50 MB), skipping")
            buffer.close()
            with _upload_lock:
                _uploading.discard(cache_key)
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


# ── Streaming helpers ─────────────────────────────────────────────────────
CHUNK = 8 * 1024


def _pass_through(stream_url: str, content_type: str):
    """
    Doorway: pipes bytes straight from YouTube to the user.
    No buffering, no storing. Just forward chunks as they arrive.
    """
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


def _download_and_upload(stream_url: str, content_type: str, query: str):
    """
    Background job: downloads the ENTIRE file from YouTube,
    then uploads it to Telegram. Completely separate from streaming.
    """
    cache_key = query.lower()

    with _upload_lock:
        if cache_key in _uploading:
            log(f"[UPLOAD] Already downloading/uploading '{query}', skipping")
            return
        _uploading.add(cache_key)

    def downloader():
        buffer = io.BytesIO()
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (compatible; audio-proxy/1.0)'}
            resp = requests.get(stream_url, headers=headers, stream=True, timeout=(10, 300))
            resp.raise_for_status()

            for chunk in resp.iter_content(chunk_size=CHUNK):
                if chunk:
                    buffer.write(chunk)

            log(f"[UPLOAD] ✅ Downloaded '{query}' — {buffer.tell():,} bytes")
            _upload_to_telegram(buffer, query, cache_key, content_type)

        except Exception as exc:
            log(f"[UPLOAD] ❌ Failed '{query}': {type(exc).__name__}: {exc}")
            buffer.close()
            with _upload_lock:
                _uploading.discard(cache_key)

    threading.Thread(target=downloader, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return "Usage: GET /audio?q=Artist+-+Song+Title", 200


@app.route('/audio', methods=['GET', 'HEAD'])
def get_audio():
    """
    1. If song is in Supabase → redirect to Telegram CDN (fastest)
    2. Otherwise → pass-through stream from YouTube (instant playback)
       + start background download for Telegram upload
    """
    query = request.args.get('q', '').strip()
    if not query:
        return "Error: missing q parameter", 400

    cache_key = query.lower()
    log(f"[AUDIO] Request: '{query}'")

    # ── 1. Supabase — redirect straight to Telegram if we have it ────────
    row = supabase_lookup(query)
    if row:
        log(f"[AUDIO] Found in Supabase: {row}")
        tg_url = telegram_get_stream_url(row["file_id"])
        if tg_url:
            log(f"[AUDIO] Redirecting to Telegram CDN: {query}")
            return redirect(tg_url, code=302)
        log(f"[AUDIO] Telegram URL failed, falling back to YouTube")
    else:
        log(f"[AUDIO] Not found in Supabase, will use YouTube")

    # ── 2. Resolve YouTube URL ────────────────────────────────────────────
    stream_url, ct = _cache_get(cache_key)
    if not stream_url:
        log(f"[AUDIO] Scraping YouTube for: {query}")
        try:
            stream_url, ct = resolve_youtube(query, cache_key)
        except Exception as exc:
            return f"Error resolving stream: {exc}", 500
        if not stream_url:
            return "Error: no audio stream found", 404

    # ── 3. Start background download for Telegram upload ─────────────────
    _download_and_upload(stream_url, ct or 'audio/webm', query)

    # ── 4. Stream to user immediately (pass-through, no buffering) ──────
    log(f"[AUDIO] Streaming pass-through: {query}")
    return _pass_through(stream_url, ct or 'audio/webm')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
