import os
import re
import requests
from io import BytesIO
from flask import Flask, request, Response
import yt_dlp

app = Flask(__name__)

# Simple in-memory cache: { video_url: (audio_bytes, content_type) }
# Holds up to 10 songs so your phone isn't re-downloading the same track
CACHE = {}
CACHE_MAX = 10

YTMUSIC_AVAILABLE = False
try:
    from ytmusicapi import YTMusic
    ytm = YTMusic()
    YTMUSIC_AVAILABLE = True
except ImportError:
    ytm = None


def search_youtube_music(query):
    if YTMUSIC_AVAILABLE:
        results = ytm.search(query, filter="songs", limit=1)
        if not results:
            results = ytm.search(query, limit=1)
        if results:
            video_id = results[0].get("videoId")
            if video_id:
                return f"https://music.youtube.com/watch?v={video_id}"
        return None
    else:
        search_query = f"ytsearch1:{query}"
        ydl_opts = {
            'quiet': True,
            'skip_download': True,
            'extract_flat': True,
            'extractor_args': {
                'youtube': {'player_client': ['web_music']},
            },
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            entries = info.get('entries', [])
            if not entries:
                return None
            return entries[0]['url']


def get_audio_stream_url(video_url):
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'skip_download': True,
        'extract_flat': False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        formats = info.get('formats', [])
        audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
        if not audio_formats:
            audio_formats = [f for f in formats if f.get('acodec') != 'none']
        if not audio_formats:
            return None, None
        audio_formats.sort(key=lambda x: x.get('abr', 0) or x.get('tbr', 0) or 0, reverse=True)
        best = audio_formats[0]
        return best['url'], best.get('ext', 'webm')


def fetch_full_audio(video_url):
    """
    Download the entire song into memory and cache it.
    Next person who requests the same song gets it instantly.
    """
    if video_url in CACHE:
        return CACHE[video_url]

    stream_url, ext = get_audio_stream_url(video_url)
    if not stream_url:
        return None, None

    # Download the whole thing at once — no chunk-by-chunk relay
    upstream = requests.get(
        stream_url,
        headers={'User-Agent': 'Mozilla/5.0 (compatible; yt-dlp)'},
        timeout=60,
    )
    upstream.raise_for_status()

    audio_bytes = upstream.content
    content_type = upstream.headers.get('Content-Type', 'audio/webm')

    # Evict oldest entry if cache is full
    if len(CACHE) >= CACHE_MAX:
        oldest_key = next(iter(CACHE))
        del CACHE[oldest_key]

    CACHE[video_url] = (audio_bytes, content_type)
    return audio_bytes, content_type


@app.route('/')
def home():
    return "Usage: GET /audio?q=SONG+NAME+AND+ARTIST", 200


@app.route('/audio', methods=['GET'])
def get_audio():
    query = request.args.get('q')
    if not query:
        return "Error: Missing q parameter", 400

    try:
        if re.match(r'https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/.+', query):
            video_url = query
        else:
            video_url = search_youtube_music(query)
            if not video_url:
                return "Error: No search results found", 404

        audio_bytes, content_type = fetch_full_audio(video_url)
        if not audio_bytes:
            return "Error: No audio stream found", 404

        # Handle Range requests so seeking still works
        total = len(audio_bytes)
        range_header = request.headers.get('Range')

        if range_header:
            # Parse "bytes=start-end"
            match = re.match(r'bytes=(\d+)-(\d*)', range_header)
            if match:
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else total - 1
                end = min(end, total - 1)
                chunk = audio_bytes[start:end + 1]
                resp = Response(chunk, status=206, mimetype=content_type)
                resp.headers['Content-Range'] = f'bytes {start}-{end}/{total}'
                resp.headers['Content-Length'] = str(len(chunk))
                resp.headers['Accept-Ranges'] = 'bytes'
                resp.headers['Access-Control-Allow-Origin'] = '*'
                return resp

        # No range — send the whole thing at once
        resp = Response(audio_bytes, status=200, mimetype=content_type)
        resp.headers['Content-Length'] = str(total)
        resp.headers['Accept-Ranges'] = 'bytes'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp

    except Exception as e:
        return f"Error: {str(e)}", 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)