import os
import re
from flask import Flask, request, Response
import yt_dlp

app = Flask(__name__)

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
        
        # Sort by quality and grab the best one
        audio_formats.sort(key=lambda x: x.get('abr', 0) or x.get('tbr', 0) or 0, reverse=True)
        return audio_formats[0]['url']

@app.route('/')
def home():
    return "Usage: GET /audio?url=YOUR_YOUTUBE_URL", 200

@app.route('/audio', methods=['GET'])
def get_audio():
    video_url = request.args.get('url')
    
    if not video_url:
        return "Error: Missing url parameter", 400
    
    if not re.match(r'https?://(www\.)?(youtube\.com|youtu\.be)/.+', video_url):
        return "Error: Invalid YouTube URL", 400
    
    try:
        stream_url = get_audio_stream_url(video_url)
        
        if not stream_url:
            return "Error: No audio stream found", 404
        
        # Return as plain text with CORS header so the browser can fetch it
        response = Response(stream_url, mimetype='text/plain')
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
        
    except Exception as e:
        return f"Error: {str(e)}", 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)