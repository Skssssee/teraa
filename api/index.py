import urllib.parse
import base64
import requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import os
import uuid
import json
from pymongo import MongoClient

app = Flask(__name__)
CORS(app)

# MongoDB Configuration
MONGO_URI = os.environ.get("MONGO_URI", "")
db_client = None
db = None
api_collection = None

if MONGO_URI:
    try:
        db_client = MongoClient(MONGO_URI)
        db = db_client['terastream_db']
        api_collection = db['api_pool']
    except Exception as e:
        print("MongoDB connection error:", e)

def get_active_api():
    if not api_collection:
        # Fallback to the original friend API if MongoDB is not connected
        return "https://tera-download-rose.vercel.app/api/extract"
        
    active = api_collection.find_one({"active": True})
    if active:
        return active.get("url")
        
    # If no active API is found, fallback
    return "https://tera-download-rose.vercel.app/api/extract"


@app.route('/')
def home():
    try:
        # Guarantee the frontend loads by explicitly serving index.html from the root folder
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        index_path = os.path.join(root_dir, 'index.html')
        with open(index_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"Frontend Error: {str(e)}", 500

@app.route('/admin')
def admin_panel():
    try:
        # Guarantee the admin panel loads by explicitly serving it
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        admin_path = os.path.join(root_dir, 'admin123.html')
        with open(admin_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"Admin Panel Error: {str(e)}", 500

@app.route('/api/resolve', methods=['GET'])
def resolve():
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "Missing URL parameter", "errno": -1}), 400
        
    try:
        # Dynamically fetch the active API from MongoDB
        active_api_base = get_active_api()
        
        # Properly encode the URL so the friend API doesn't crash
        encoded_url = urllib.parse.quote(url, safe='')
        
        # Ensure we append ?url= properly
        if "?" in active_api_base:
            friend_api_url = f"{active_api_base}&url={encoded_url}"
        else:
            friend_api_url = f"{active_api_base}?url={encoded_url}"

        
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


# ==========================================
# ADMIN PANEL ENDPOINTS (API MANAGER)
# ==========================================
def verify_admin(req):
    auth_header = req.headers.get("Authorization")
    if not auth_header:
        return False
    try:
        auth_type, credentials = auth_header.split(" ")
        decoded = base64.b64decode(credentials).decode("utf-8")
        username, password = decoded.split(":")
        
        # Standard admin credentials (could be env vars)
        admin_user = os.environ.get("ADMIN_USER", "admin123")
        admin_pass = os.environ.get("ADMIN_PASS", "admin123")
        
        return username == admin_user and password == admin_pass
    except Exception:
        return False

@app.route('/api/admin/apis', methods=['GET'])
def get_apis():
    if not verify_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
        
    if not api_collection:
        return jsonify({
            "source": "No Database",
            "status": "inactive",
            "pool": []
        })
        
    apis = list(api_collection.find({}, {"_id": 0}))
    return jsonify({
        "source": "MongoDB",
        "status": "active",
        "pool": apis
    })

@app.route('/api/admin/apis', methods=['POST'])
def add_or_update_api():
    if not verify_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
        
    if not api_collection:
        return jsonify({"error": "MongoDB is not configured"}), 500
        
    data = request.json
    api_id = data.get('id')
    name = data.get('name', 'Unnamed API')
    url = data.get('url', '')
    active = data.get('active', False)
    
    if not url:
        return jsonify({"error": "URL is required"}), 400
        
    # If this one is set to active, deactivate all others first
    if active:
        api_collection.update_many({}, {"$set": {"active": False}})
        
    if api_id:
        # Edit existing
        api_collection.update_one(
            {"id": api_id},
            {"$set": {"name": name, "url": url, "active": active}}
        )
    else:
        # Create new
        api_id = uuid.uuid4().hex
        api_collection.insert_one({
            "id": api_id,
            "name": name,
            "url": url,
            "active": active
        })
        
    return jsonify({"success": True, "id": api_id})

@app.route('/api/admin/apis/<api_id>', methods=['DELETE'])
def delete_api(api_id):
    if not verify_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
        
    if not api_collection:
        return jsonify({"error": "MongoDB is not configured"}), 500
        
    api_collection.delete_one({"id": api_id})
    return jsonify({"success": True})

@app.route('/api/admin/apis/<api_id>/active', methods=['POST'])
def set_active_api(api_id):
    if not verify_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
        
    if not api_collection:
        return jsonify({"error": "MongoDB is not configured"}), 500
        
    # Deactivate all
    api_collection.update_many({}, {"$set": {"active": False}})
    
    # Activate the target
    api_collection.update_one({"id": api_id}, {"$set": {"active": True}})
    
    return jsonify({"success": True})
