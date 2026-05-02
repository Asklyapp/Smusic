import os
import re
import requests
from flask import Flask, request, Response

app = Flask(__name__)

# Piped instance - these are volunteer-run proxies that handle YouTube extraction
# You can swap this out if it goes down. List: https://github.com/TeamPiped/Piped/wiki/Instances
PIPED_API = "https://pipedapi.kavin.rocks"


def search_piped(query):
    """Search for music via Piped API and return the first result video ID."""
    try:
        resp = requests.get(
            f"{PIPED_API}/search",
            params={"q": query, "filter": "music_songs"},
            timeout=10
        )
        resp.raise_for_status()
        results = resp.json().get("items", [])
        if results:
            return results[0].get("url")  # e.g. /watch?v=VIDEO_ID
        return None
    except Exception:
        return None


def get_audio_stream(video_id):
    """Get direct audio stream URL from Piped for a given video ID."""
    try:
        resp = requests.get(
            f"{PIPED_API}/streams/{video_id}",
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        audio_streams = data.get("audioStreams", [])
        if not audio_streams:
            return None
        # Sort by bitrate, highest first
        audio_streams.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
        return audio_streams[0].get("url")
    except Exception:
        return None


@app.route("/")
def home():
    return "Usage: GET /audio?q=SONG+NAME+AND+ARTIST", 200


@app.route("/audio", methods=["GET"])
def get_audio():
    query = request.args.get("q")
    if not query:
        return "Error: Missing q parameter", 400

    try:
        # If it looks like a YouTube URL, extract video ID
        url_match = re.match(
            r"https?://(www\.)?(youtube\.com/watch\?v=|youtu\.be/|music\.youtube\.com/watch\?v=)([a-zA-Z0-9_-]+)",
            query,
        )
        if url_match:
            video_id = url_match.group(3)
        else:
            # Search Piped for the song
            video_url = search_piped(query)
            if not video_url:
                return "Error: No search results found", 404
            # Extract video ID from /watch?v=VIDEO_ID
            video_id = video_url.split("v=")[-1] if "v=" in video_url else video_url.split("/")[-1]

        # Get direct audio stream URL from Piped
        stream_url = get_audio_stream(video_id)
        if not stream_url:
            return "Error: No audio stream found", 404

        # Return the direct URL to the client - their phone plays it directly
        response = Response(stream_url, mimetype="text/plain")
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    except Exception as e:
        return f"Error: {str(e)}", 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
