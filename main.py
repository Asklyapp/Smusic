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

PLAYER_CLIENTS = ['tv_embedded', 'ios', 'android', 'web_safari', 'web']

BASE_YDL_OPTS = {
    'quiet': True,
    'skip_download': True,
    'extract_flat': False,
    'cookiefile': 'cookies.txt',
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    },
}


def try_extract(video_url, client):
    """Try extracting info using a specific player client."""
    ydl_opts = {
        **BASE_YDL_OPTS,
        'extractor_args': {
            'youtube': {
                'player_client': [client],
            }
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(video_url, download=False)


def pick_best_audio(formats):
    """Return the best audio-only format, or best format with audio as fallback."""
    audio_only = [
        f for f in formats
        if f.get('acodec') != 'none' and f.get('vcodec') == 'none' and f.get('url')
    ]
    if audio_only:
        audio_only.sort(key=lambda x: x.get('abr') or x.get('tbr') or 0, reverse=True)
        return audio_only[0]

    # Fallback: any format that has audio
    has_audio = [f for f in formats if f.get('acodec') != 'none' and f.get('url')]
    if has_audio:
        has_audio.sort(key=lambda x: x.get('abr') or x.get('tbr') or 0, reverse=True)
        return has_audio[0]

    return None


def get_audio_stream_url(video_url):
    """Try each player client in order, return first successful result."""
    errors = []

    for client in PLAYER_CLIENTS:
        try:
            info = try_extract(video_url, client)
            formats = info.get('formats', [])
            best = pick_best_audio(formats)

            if not best:
                errors.append(f'{client}: got {len(formats)} formats but none had audio')
                continue

            return {
                'stream_url': best['url'],
                'format_id': best['format_id'],
                'ext': best['ext'],
                'abr': best.get('abr'),
                'asr': best.get('asr'),
                'title': info.get('title'),
                'duration': info.get('duration'),
                'thumbnail': info.get('thumbnail'),
                'player_client': client,
            }

        except Exception as e:
            errors.append(f'{client}: {str(e)}')
            continue

    raise Exception('All clients failed. Details: ' + ' | '.join(errors))


@app.route('/')
def home():
    return jsonify({
        'message': 'YouTube Audio Stream API',
        'endpoints': {
            'GET /audio?url=YOUTUBE_URL': 'Get best audio stream URL',
            'GET /formats?url=YOUTUBE_URL': 'Debug: list all available formats',
        }
    })


@app.route('/audio', methods=['GET'])
def get_audio():
    video_url = request.args.get('url')

    if not video_url:
        return jsonify({'error': "Missing 'url' parameter"}), 400

    if not re.match(r'https?://(www\.)?(youtube\.com|youtu\.be)/.+', video_url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400

    try:
        result = get_audio_stream_url(video_url)
        return jsonify({
            'success': True,
            'title': result['title'],
            'stream_url': result['stream_url'],
            'player_client_used': result['player_client'],
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


@app.route('/formats', methods=['GET'])
def list_formats():
    """Debug endpoint — shows every format YouTube returns for a video."""
    video_url = request.args.get('url')

    if not video_url:
        return jsonify({'error': "Missing 'url' parameter"}), 400

    results = {}
    for client in PLAYER_CLIENTS:
        try:
            info = try_extract(video_url, client)
            formats = info.get('formats', [])
            results[client] = {
                'format_count': len(formats),
                'formats': [
                    {
                        'id': f.get('format_id'),
                        'ext': f.get('ext'),
                        'acodec': f.get('acodec'),
                        'vcodec': f.get('vcodec'),
                        'abr': f.get('abr'),
                        'tbr': f.get('tbr'),
                        'has_url': bool(f.get('url')),
                    }
                    for f in formats
                ]
            }
        except Exception as e:
            results[client] = {'error': str(e)}

    return jsonify(results)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
