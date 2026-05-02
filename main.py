import os
import re
import requests
from flask import Flask, request, Response
import yt_dlp

app = Flask(__name__)

# Try to import ytmusicapi; if not installed, fall back to ytsearch with web_music
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
    """Extract the best audio-only stream URL from a YouTube video."""
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
            return None
        audio_formats.sort(key=lambda x: x.get('abr', 0) or x.get('tbr', 0) or 0, reverse=True)
        return audio_formats[0]['url']


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

        stream_url = get_audio_stream_url(video_url)
        if not stream_url:
            return "Error: No audio stream found", 404

        # Build headers to mimic a real browser — critical for YouTube not to 403 us
        youtube_headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'identity',  # avoid gzip so we can stream raw bytes
            'Referer': 'https://music.youtube.com/',
            'Origin': 'https://music.youtube.com',
            'Connection': 'keep-alive',
        }

        range_header = request.headers.get('Range')
        if range_header:
            youtube_headers['Range'] = range_header

        youtube_response = requests.get(
            stream_url,
            headers=youtube_headers,
            stream=True,
            timeout=30
        )

        # If YouTube rejects us, log it and return the error
        if youtube_response.status_code not in (200, 206):
            return f"Error: Upstream returned {youtube_response.status_code}", 502

        def generate():
            # Use raw stream to avoid requests decoding chunks
            for chunk in youtube_response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk

        response = Response(
            generate(),
            status=youtube_response.status_code,
            content_type=youtube_response.headers.get('Content-Type', 'audio/webm')
        )

        # Forward headers that matter for media playback
        for h in ('Content-Range', 'Accept-Ranges', 'Content-Length', 'Cache-Control', 'ETag', 'Last-Modified'):
            val = youtube_response.headers.get(h)
            if val:
                response.headers[h] = val

        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Expose-Headers'] = 'Content-Range, Accept-Ranges, Content-Length'
        return response

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Error: {str(e)}", 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
