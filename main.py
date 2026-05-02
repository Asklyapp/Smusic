import os
import re
import requests
from flask import Flask, request, Response, stream_with_context
import yt_dlp

app = Flask(__name__)

# Try to import ytmusicapi; if not installed, fall back to ytsearch
YTMUSIC_AVAILABLE = False
try:
    from ytmusicapi import YTMusic
    ytm = YTMusic()
    YTMUSIC_AVAILABLE = True
except ImportError:
    ytm = None


def search_youtube_music(query):
    """Search YouTube Music for a query and return the top result video URL."""
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
                'youtube': {
                    'player_client': ['web_music'],
                },
            },
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            entries = info.get('entries', [])
            if not entries:
                return None
            return entries[0]['url']


def get_audio_stream_url(video_url):
    """
    Extract the best audio-only stream URL from a YouTube video.
    The URL returned is bound to THIS server's IP — do not send it
    directly to clients or it will be rejected by YouTube.
    """
    ydl_opts = {
        'format': 'bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'skip_download': True,
        'extract_flat': False,
        # Use the iOS client — far less likely to get geo/token issues
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'web_music'],
            },
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        formats = info.get('formats', [])

        # Prefer audio-only formats
        audio_formats = [
            f for f in formats
            if f.get('acodec') != 'none' and f.get('vcodec') == 'none'
        ]
        if not audio_formats:
            audio_formats = [f for f in formats if f.get('acodec') != 'none']
        if not audio_formats:
            return None, None

        audio_formats.sort(
            key=lambda x: x.get('abr', 0) or x.get('tbr', 0) or 0,
            reverse=True
        )
        best = audio_formats[0]
        return best['url'], best.get('ext', 'webm')


@app.route('/')
def home():
    return "Usage: GET /audio?q=SONG+NAME+AND+ARTIST", 200


@app.route('/audio', methods=['GET'])
def get_audio():
    query = request.args.get('q')
    if not query:
        return "Error: Missing q parameter", 400

    try:
        # Direct URL or search
        if re.match(r'https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/.+', query):
            video_url = query
        else:
            video_url = search_youtube_music(query)
            if not video_url:
                return "Error: No search results found", 404

        stream_url, ext = get_audio_stream_url(video_url)
        if not stream_url:
            return "Error: No audio stream found", 404

        # ----------------------------------------------------------------
        # KEY FIX: Proxy the stream through THIS server.
        #
        # YouTube binds the stream URL to the IP that extracted it (the
        # server). If we hand the raw URL to the client, their browser
        # hits YouTube from a different IP and gets blocked.
        # By streaming through here, every byte goes server → client and
        # YouTube only ever sees the server's IP. ✅
        # ----------------------------------------------------------------

        # Forward any Range header the client sent (needed for seeking)
        range_header = request.headers.get('Range')
        upstream_headers = {
            'User-Agent': (
                'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 '
                'Mobile/15E148 Safari/604.1'
            ),
        }
        if range_header:
            upstream_headers['Range'] = range_header

        upstream = requests.get(
            stream_url,
            headers=upstream_headers,
            stream=True,
            timeout=10,
        )

        # Pick a sensible Content-Type
        content_type_map = {
            'webm': 'audio/webm',
            'm4a': 'audio/mp4',
            'mp4': 'audio/mp4',
            'ogg': 'audio/ogg',
            'opus': 'audio/ogg',
        }
        content_type = content_type_map.get(ext, 'audio/webm')

        # Build response headers to pass back to the client
        resp_headers = {
            'Content-Type': content_type,
            'Access-Control-Allow-Origin': '*',
            'Accept-Ranges': 'bytes',
        }
        # Forward content-length and content-range if YouTube sent them
        for h in ('Content-Length', 'Content-Range'):
            val = upstream.headers.get(h)
            if val:
                resp_headers[h] = val

        status_code = upstream.status_code  # 200 or 206 (partial content)

        def generate():
            for chunk in upstream.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        return Response(
            stream_with_context(generate()),
            status=status_code,
            headers=resp_headers,
        )

    except Exception as e:
        return f"Error: {str(e)}", 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)