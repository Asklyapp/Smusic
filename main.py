import os
import re
import requests
from flask import Flask, request, Response

app = Flask(__name__)

# Working Invidious instances as of May 2026
# Tested and confirmed working by the community
INVIDIOUS_INSTANCES = [
    "https://invidious.darkness.services",
    "https://invidious.fdn.fr",
    "https://invidious.flokinet.to",
]


def search_invidious(query):
    """Search for music via Invidious API and return the first result video ID."""
    for instance in INVIDIOUS_INSTANCES:
        try:
            resp = requests.get(
                f"{instance}/api/v1/search",
                params={"q": query, "type": "video"},
                timeout=15
            )
            resp.raise_for_status()
            results = resp.json()
            if results and len(results) > 0:
                video_id = results[0].get("videoId")
                if video_id:
                    return video_id, instance
        except Exception as e:
            print(f"[search] {instance} failed: {e}")
            continue
    return None, None


def get_audio_stream(video_id, instance):
    """Get direct audio stream URL from Invidious for a given video ID."""
    try:
        resp = requests.get(
            f"{instance}/api/v1/videos/{video_id}",
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()

        # adaptiveFormats contains separate audio and video streams
        formats = data.get("adaptiveFormats", [])
        audio_formats = [f for f in formats if f.get("type", "").startswith("audio/")]

        if not audio_formats:
            # Fallback: try all formats and filter by mime type
            formats = data.get("formatStreams", [])
            audio_formats = [f for f in formats if f.get("type", "").startswith("audio/")]

        if not audio_formats:
            return None

        # Sort by bitrate, highest first
        audio_formats.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
        return audio_formats[0].get("url")
    except Exception as e:
        print(f"[stream] {instance} failed for {video_id}: {e}")
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
            instance = INVIDIOUS_INSTANCES[0]
        else:
            # Search Invidious for the song
            video_id, instance = search_invidious(query)
            if not video_id:
                return "Error: No search results found (all Invidious instances failed)", 404

        # Get direct audio stream URL from Invidious
        stream_url = get_audio_stream(video_id, instance)
        if not stream_url:
            return "Error: No audio stream found", 404

        # Return the direct URL to the client - their phone plays it directly
        response = Response(stream_url, mimetype="text/plain")
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Error: {str(e)}", 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
