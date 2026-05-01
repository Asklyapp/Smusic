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
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'extract_flat': False,
        'cookiefile': 'cookies.txt',
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)

        # Find the best audio-only format
        formats = info.get('formats', [])
        audio_formats = [
    f for f in formats
    if f.get('acodec') != 'none'
]

if not audio_formats:
    return None

audio_formats.sort(
    key=lambda x: (
        x.get('abr') or 0,
        x.get('tbr') or 0,
        x.get('filesize') or 0
    ),
    reverse=True
)

best_audio = audio_formats[0]

        return {
            'stream_url': best_audio['url'],
            'format_id': best_audio['format_id'],
            'ext': best_audio['ext'],
            'abr': best_audio.get('abr'),
            'asr': best_audio.get('asr'),
            'title': info.get('title'),
            'duration': info.get('duration'),
            'thumbnail': info.get('thumbnail'),
        }


@app.route('/')
def home():
    return jsonify({
        'message': 'YouTube Audio Stream API',
        'usage': 'GET /audio?url=YOUR_YOUTUBE_URL'
    })


@app.route('/audio', methods=['GET'])
def get_audio():
    video_url = request.args.get('url')

    if not video_url:
        return jsonify({'error': "Missing 'url' parameter. Usage: /audio?url=YOUR_YOUTUBE_URL"}), 400

    # Basic URL validation
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
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
