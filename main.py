"""
YouTube Audio Stream URL API
Simple Flask API that returns direct audio stream URLs from YouTube videos.
Ready to deploy on Render.
"""

import os
import re
from flask import Flask, request, jsonify
import yt_dlp

app = Flask(__name__)


def get_audio_stream_url(video_url):
    """Extract the best audio-only stream URL from a YouTube video."""
    # Updated options to bypass format availability errors and use cookies
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'extract_flat': False,
        'cookiefile': 'cookies.txt',  # References your provided cookie file[span_1](start_span)[span_1](end_span)
        'format': 'bestaudio/best',   # Forces yt-dlp to find the highest quality audio[span_2](start_span)[span_2](end_span)
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
        }
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # Extract info using the optimized options[span_3](start_span)[span_3](end_span)
        info = ydl.extract_info(video_url, download=False)

        # When 'format' is set to 'bestaudio', yt-dlp populates 'url' with the direct stream link[span_4](start_span)[span_4](end_span)
        stream_url = info.get('url')
        
        if not stream_url:
            return None

        return {
            'stream_url': stream_url,
            'format_id': info.get('format_id'),
            'ext': info.get('ext'),
            'abr': info.get('abr') or info.get('tbr'),
            'asr': info.get('asr'),
            'title': info.get('title'),
            'duration': info.get('duration'),
            'thumbnail': info.get('thumbnail'),
        }


@app.route('/')
def home():
    """API Root Route[span_5](start_span)[span_5](end_span)"""
    return jsonify({
        'message': 'YouTube Audio Stream API',
        'usage': 'GET /audio?url=YOUR_YOUTUBE_URL'
    })


@app.route('/audio', methods=['GET'])
def get_audio():
    """Endpoint to fetch audio stream details[span_6](start_span)[span_6](end_span)"""
    video_url = request.args.get('url')

    if not video_url:
        return jsonify({'error': "Missing 'url' parameter. Usage: /audio?url=YOUR_YOUTUBE_URL"}), 400

    # Basic URL validation[span_7](start_span)[span_7](end_span)
    if not re.match(r'https?://(www\.)?(youtube\.com|youtu\.be)/.+', video_url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400

    try:
        result = get_audio_stream_url(video_url)

        if not result:
            return jsonify({'error': 'No audio stream found'}), 404

        return jsonify({
            'success': True,
            'title': result['title'],
            'stream_url': result['stream_url'],
            'format': {
                'id': result['format_id'],
                'ext': result['ext'],
                'bitrate': result['abr'],
                'sample_rate': result['asr'],
            },
            'duration': result['duration'],
            'thumbnail': result['thumbnail'],
        })

    except Exception as e:
        # Catching and returning the specific yt-dlp error[span_8](start_span)[span_8](end_span)
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    # Configuration for Render deployment[span_9](start_span)[span_9](end_span)
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
