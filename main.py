"""
YouTube Audio Stream URL API
Simple Flask API that returns direct audio stream URLs from YouTube videos.
"""

import os
import re
from pathlib import Path
from flask import Flask, request, jsonify
import yt_dlp

app = Flask(__name__)

PLAYER_CLIENTS = ['tv_embedded', 'ios', 'android', 'web_safari', 'web']

SCRIPT_DIR = Path(__file__).parent
COOKIES_FILE = str(SCRIPT_DIR / 'cookies.txt')


def try_extract(video_url, client):
    cookies = COOKIES_FILE if Path(COOKIES_FILE).exists() else None

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'format': 'bestaudio/best',
        'extractor_args': {
            'youtube': {
                'player_client': [client],
            }
        },
    }

    if cookies:
        ydl_opts['cookiefile'] = cookies

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(video_url, download=False)


def get_audio_stream_url(video_url):
    errors = []

    for client in PLAYER_CLIENTS:
        try:
            info = try_extract(video_url, client)

            # yt-dlp resolves the best format into the top-level 'url'
            url = info.get('url')

            # If not top-level, dig into formats list
            if not url:
                formats = info.get('formats', [])
                audio = [f for f in formats if f.get('acodec') != 'none' and f.get('url')]
                if not audio:
                    errors.append(f'{client}: no audio formats found')
                    continue
                audio.sort(key=lambda x: x.get('abr') or x.get('tbr') or 0, reverse=True)
                best = audio[0]
                url = best['url']
                ext = best.get('ext', 'webm')
                abr = best.get('abr')
                asr = best.get('asr')
                fmt_id = best.get('format_id', 'unknown')
            else:
                ext = info.get('ext', 'webm')
                abr = info.get('abr')
                asr = info.get('asr')
                fmt_id = info.get('format_id', 'unknown')

            return {
                'stream_url': url,
                'format_id': fmt_id,
                'ext': ext,
                'abr': abr,
                'asr': asr,
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
        'cookies_found': Path(COOKIES_FILE).exists(),
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
    video_url = request.args.get('url')
    if not video_url:
        return jsonify({'error': "Missing 'url' parameter"}), 400

    results = {}
    for client in PLAYER_CLIENTS:
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
            }
            if Path(COOKIES_FILE).exists():
                ydl_opts['cookiefile'] = COOKIES_FILE

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=False)
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
