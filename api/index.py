import urllib.parse
import base64
import requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

@app.route('/api/resolve', methods=['GET'])
def resolve():
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "Missing URL parameter", "errno": -1}), 400
        
    try:
        # Properly encode the URL so the friend API doesn't crash
        encoded_url = urllib.parse.quote(url, safe='')
        friend_api_url = f"https://tera-download-rose.vercel.app/api/extract?url={encoded_url}"
        
        f_res = requests.get(friend_api_url, timeout=10)
        if f_res.status_code != 200:
            return jsonify({"error": f"Friend API returned status {f_res.status_code}", "errno": -1}), 500
            
        f_data = f_res.json()
        
        if f_data.get("success") and "data" in f_data and "streams" in f_data["data"]:
            streams = f_data["data"]["streams"]
            best_stream = streams.get("480p") or streams.get("360p") or streams.get("720p") or streams.get("1080p")
            
            # Crack the Base64 security layer
            if best_stream and 'api/redirect?data=' in best_stream:
                try:
                    b64_data = best_stream.split('api/redirect?data=')[1]
                    b64_data += "=" * ((4 - len(b64_data) % 4) % 4)
                    best_stream = base64.b64decode(b64_data).decode('utf-8')
                except Exception:
                    pass
            
            if best_stream:
                # Point the player to our hidden proxy
                proxy_url = f"/api/m3u8?url={urllib.parse.quote(best_stream, safe='')}"
                
                return jsonify({
                    "errno": 0,
                    "list": [
                        {
                            "server_filename": f_data["data"].get("file_name", "video.mp4"),
                            "dlink": proxy_url,
                            "thumbs": {
                                "url3": f_data["data"].get("thumbnail", "")
                            }
                        }
                    ]
                })
        
        return jsonify({"error": "No streams found in friend API", "data": f_data, "errno": -1}), 500
        
    except Exception as e:
        return jsonify({"error": f"Friend API crashed: {str(e)}", "errno": -1}), 500


@app.route('/api/m3u8', methods=['GET'])
def proxy_m3u8():
    url = request.args.get('url')
    if not url:
        return "Missing URL", 400
    try:
        # Secretly download and proxy the .m3u8 file
        r = requests.get(url, timeout=10)
        return Response(r.text, content_type='application/vnd.apple.mpegurl', headers={
            'Access-Control-Allow-Origin': '*'
        })
    except Exception as e:
        return str(e), 500
