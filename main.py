import os
import re
import time
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
            return e['url'], e.get('ct', 'audio/mp4')
    return None, None

def _cache_set(key: str, url: str, ct: str = 'audio/mp4'):
    with _cache_lock:
        _cache[key] = {'url': url, 'ts': time.time(), 'ct': ct}


def log(msg: str):
    print(msg, flush=True)


# ── Supabase helpers ──────────────────────────────────────────────────────

def supabase_lookup(query: str) -> dict | None:
    try:
        q = query.strip().lower()
        params = {"query": f"ilike.{q}", "limit": 1}
        resp = requests.get(SUPABASE_TABLE, headers=SUPABASE_HEADERS, params=params, timeout=5)
        rows = resp.json()
        if isinstance(rows, list) and rows:
            return rows[0]

        parts = re.split(r'\s*-\s*', q, maxsplit=1)
        if len(parts) == 2:
            artist, title = parts[0].strip(), parts[1].strip()
            params = {"artist": f"ilike.*{artist}*", "title": f"ilike.*{title}*", "limit": 1}
            resp = requests.get(SUPABASE_TABLE, headers=SUPABASE_HEADERS, params=params, timeout=5)
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
            "query": query.strip().lower(), "title": title,
            "artist": artist, "file_id": file_id, "content_type": content_type,
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
        log(f"[TELEGRAM] ❌ getFile exception: {exc}")
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


def _upload_to_telegram(buffer: io.BytesIO, query: str, cache_key: str, content_type: str):
    try:
        size = buffer.tell()
        if size < 1024:
            log(f"[TELEGRAM] ❌ Too small ({size} bytes), skipping")
            return
        if size > TELEGRAM_MAX_BYTES:
            log(f"[TELEGRAM] ❌ Too large ({size:,} bytes), skipping")
            return
        ext = ('m4a'  if 'mp4'  in content_type else
               'webm' if 'webm' in content_type else
               'mp3'  if 'mpeg' in content_type else 'm4a')
        file_id, err = telegram_upload_buffer(buffer, filename=f"{query.replace(' ', '_')}.{ext}")
        if err:
            log(f"[TELEGRAM] ❌ Upload failed: {err}")
        else:
            log(f"[TELEGRAM] ✅ Uploaded! file_id={file_id}")
            supabase_save(query, file_id, content_type)
    except Exception as exc:
        log(f"[TELEGRAM] ❌ Exception: {exc}")
    finally:
        buffer.close()
        with _upload_lock:
            _uploading.discard(cache_key)


# ── yt-dlp helpers ────────────────────────────────────────────────────────
YTMUSIC_AVAILABLE = False
try:
    from ytmusicapi import YTMusic
    ytm = YTMusic()
    YTMUSIC_AVAILABLE = True
except ImportError:
    ytm = None

_MIME = {'webm': 'audio/webm', 'm4a': 'audio/mp4',
         'mp4': 'audio/mp4', 'ogg': 'audio/ogg', 'mp3': 'audio/mpeg'}

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
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio',
        'quiet': True, 'skip_download': True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        fmts = info.get('formats', [])
        # Audio-only formats only — never pick a video+audio combined format
        audio = [f for f in fmts if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
        if not audio:
            return None, None
        audio.sort(key=lambda x: x.get('abr') or x.get('tbr') or 0, reverse=True)
        best = audio[0]
        ct = _MIME.get(best.get('ext', ''), 'audio/mp4')
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


# ── Tee streaming ─────────────────────────────────────────────────────────
CHUNK = 8 * 1024


def _tee_stream(stream_url: str, content_type: str, query: str, cache_key: str):
    """
    One YouTube request. Two things happen in parallel:
      1. Chunks are yielded to the client immediately (streaming)
      2. The same chunks are written into a buffer that a background
         thread uploads to Telegram — concurrently, not after streaming finishes.

    The client never waits for Telegram. Telegram never waits for
    the client to finish. They run independently from one download.
    """
    upstream_headers = {'User-Agent': 'Mozilla/5.0 (compatible; audio-proxy/1.0)'}
    yt = requests.get(stream_url, headers=upstream_headers, stream=True, timeout=(5, None))

    resp_headers = {
        'Content-Type':                content_type,
        'Accept-Ranges':               'bytes',
        'Access-Control-Allow-Origin': '*',
        'Cache-Control':               'no-store',
    }
    if 'Content-Length' in yt.headers:
        resp_headers['Content-Length'] = yt.headers['Content-Length']

    # Decide whether to upload — skip if already in progress
    should_upload = False
    with _upload_lock:
        if cache_key not in _uploading:
            _uploading.add(cache_key)
            should_upload = True

    pipe_chunks = []
    pipe_lock   = threading.Lock()
    pipe_done   = threading.Event()

    def uploader():
        buf = io.BytesIO()
        idx = 0
        while True:
            with pipe_lock:
                new = pipe_chunks[idx:]
            for chunk in new:
                buf.write(chunk)
            idx += len(new)

            if pipe_done.is_set():
                with pipe_lock:
                    new = pipe_chunks[idx:]
                for chunk in new:
                    buf.write(chunk)
                break
            time.sleep(0.01)

        # Always upload — even if the client disconnected early.
        # The download runs to completion in generate() regardless.
        log(f"[TEE] ✅ Complete ({buf.tell():,} bytes), uploading to Telegram")
        _upload_to_telegram(buf, query, cache_key, content_type)

    if should_upload:
        threading.Thread(target=uploader, daemon=True).start()

    def generate():
        try:
            for chunk in yt.iter_content(chunk_size=CHUNK):
                if chunk:
                    if should_upload:
                        with pipe_lock:
                            pipe_chunks.append(chunk)
                    yield chunk
        except GeneratorExit:
            pass   # client disconnected — download continues until pipe_done
        finally:
            yt.close()
            pipe_done.set()

    return Response(
        stream_with_context(generate()),
        status=200,
        headers=resp_headers,
    )


# ── Routes ────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return "Usage: GET /audio?q=Artist+-+Song+Title", 200


@app.route('/audio', methods=['GET', 'HEAD'])
def get_audio():
    query = request.args.get('q', '').strip()
    if not query:
        return "Error: missing q parameter", 400

    cache_key = query.lower()
    log(f"[AUDIO] Request: '{query}'")

    supabase_result = [None]
    youtube_result  = [None, None]
    supabase_done   = threading.Event()
    youtube_done    = threading.Event()

    def do_supabase():
        supabase_result[0] = supabase_lookup(query)
        supabase_done.set()

    def do_youtube():
        url, ct = _cache_get(cache_key)
        if not url:
            try:
                url, ct = resolve_youtube(query, cache_key)
            except Exception as exc:
                log(f"[YOUTUBE] ❌ {exc}")
        youtube_result[0], youtube_result[1] = url, ct
        youtube_done.set()

    threading.Thread(target=do_supabase, daemon=True).start()
    threading.Thread(target=do_youtube,  daemon=True).start()

    supabase_done.wait()
    row = supabase_result[0]
    if row:
        tg_url = telegram_get_stream_url(row["file_id"])
        if tg_url:
            log(f"[AUDIO] ✅ Telegram CDN hit: {query}")
            return redirect(tg_url, code=302)
        log(f"[AUDIO] Telegram URL failed, falling through to YouTube")

    youtube_done.wait()
    stream_url, ct = youtube_result[0], youtube_result[1]
    if not stream_url:
        return "Error: no audio stream found", 404

    log(f"[AUDIO] Tee-streaming: '{query}'")
    return _tee_stream(stream_url, ct or 'audio/mp4', query, cache_key)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
