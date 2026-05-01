import os
import re
from flask import Flask, request, jsonify
import yt_dlp

app = Flask(__name__)

def get_audio_stream_url(video_url):
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'cookiefile': 'cookies.[span_8](start_span)txt',
        # Forces yt-dlp to use the iOS client, which often bypasses web-based bot checks[span_8](end_span)
        'extractor_args': {
            'youtube': {
                'player_client': ['ios'], 
                'skip': ['webpage']
            }
        },
        'format': 'bestaudio/best',
        'nocheckcertificate': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            return {
                'stream_url': info.get('url'),
                'title': info.get('title'),
                'format_id': info.get('format_id'),
                'ext': info.get('ext'),
                'abr': info.get('abr') or info.get('tbr'),
                'duration': info.get('duration'),
                'thumbnail': info.get('thumbnail'),
            }
    except Exception as e:
        # Rethrow to be caught by the route handler
        raise e

@app.route('/audio', methods=['GET'])
def get_audio():
    video_url = request.args.get('url')
    if not video_url:
        return jsonify({'error': "Missing 'url' parameter"}), 400

    [span_9](start_span)try:
        result = get_audio_stream_url(video_url)
        return jsonify({'success': True, **result})
    except Exception as e:
        # Detailed error message helps debug if it's an IP block[span_9](end_span)
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
