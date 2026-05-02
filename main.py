import os
import re
import requests
from flask import Flask, request, Response

app = Flask(__name__)

# Multiple Piped instances - if one fails, we can rotate
# List from https://piped-instances.kavin.rocks/
PIPED_INSTANCES = [
    "https://pipedapi.adminforge.de",
    "https://pipedapi.nosebs.ru",
    "https://pipedapi.ducks.party",
    "https://pipedapi.reallyaweso.me",
    "https://api.piped.private.coffee",
    "https://pipedapi.darkness.services",
    "https://pipedapi.kavin.rocks",
    "https://pipedapi-libre.kavin.rocks",
]


def search_piped(query):
    """Search for music via Piped API and return the first result video ID."""
    for instance in PIPED_INSTANCES:
        try:
            resp = requests.get(
                f"{instance}/search",
                params={"q": query},
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])

            # Try to find a music/video result
            for item in items:
                url = item.get("url", "")
                if "/watch?v=" in url:
                    video_id = url.split("v=")[-1].split("&")[0]
                    return video_id, instance

            # Fallback: just take first result
            if items:
                url = items[0].get("url", "")
                if "/watch?v=" in url:
                    video_id = url.split("v=")[-1].split("&")[0]
                    return video_id, instance

        except Exception:
            continue

    return None, None


def get_audio_stream(video_id, instance):
    """Get direct audio stream URL from Piped for a given video ID."""
    try:
        resp = requests.get(
            f"{instance}/streams/{video_id}",
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
            # Use first instance for stream lookup
            instance = PIPED_INSTANCES[0]
        else:
            # Search Piped for the song
            video_id, instance = search_piped(query)
            if not video_id:
                return "Error: No search results found", 404

        # Get direct audio stream URL from Piped
        stream_url = get_audio_stream(video_id, instance)
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
