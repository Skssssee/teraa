import os
import re
import urllib.parse
import uuid
import time
import json
import random
from functools import wraps
from flask import Flask, request, jsonify, Response, render_template
import requests
from pymongo import MongoClient
from flask_cors import CORS

app = Flask(__name__)
CORS(app, expose_headers=["Content-Range", "Accept-Ranges", "Content-Length", "Content-Type"])

# Server-side stores for mapping random tokens to actual URLs
resolved_links_store = {}
resolved_thumbnails_store = {}

# Local file paths for persistent session pool storage
STORED_COOKIES_JSON = os.path.join(os.path.dirname(__file__), "stored_cookies.json")

# MongoDB URI provided by the user
MONGO_URI = "mongodb+srv://sumankumar821311_db_user:e6tYVXxhD2jRTRbn@cluster0.dpt7ky6.mongodb.net/?appName=Cluster0"

def check_auth(username, password):
    """
    Validates username and password credentials.
    """
    return username == 'admin123' and password == 'suman821310'

def authenticate():
    """
    Sends a 401 response that triggers basic authentication login popup.
    """
    return Response(
        'Could not verify your access level for that URL.\n'
        'You have to login with proper credentials', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'}
    )

def requires_auth(f):
    """
    Decorator to protect routes with HTTP Basic Auth.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

def get_mongo_client():
    """
    Attempts to connect to the MongoDB URI. Returns MongoClient if active, otherwise None.
    Uses a short 2-second timeout to avoid blocking startup.
    """
    if not MONGO_URI:
        return None
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        # Ping the admin database to verify active connection
        client.admin.command('ping')
        return client
    except Exception:
        return None

def load_pool_from_mongo():
    """
    Loads the cookie pool list from MongoDB.
    """
    client = get_mongo_client()
    if not client:
        return None
    try:
        db = client["terastream"]
        col = db["settings"]
        doc = col.find_one({"_id": "session_cookies_pool"})
        if doc and "pool" in doc:
            return doc["pool"]
    except Exception:
        return None
    return None

def save_pool_to_mongo(pool):
    """
    Saves the cookie pool list to MongoDB.
    """
    client = get_mongo_client()
    if not client:
        return False
    try:
        db = client["terastream"]
        col = db["settings"]
        col.replace_one(
            {"_id": "session_cookies_pool"},
            {"_id": "session_cookies_pool", "pool": pool},
            upsert=True
        )
        return True
    except Exception:
        return False

def delete_cookies_from_mongo():
    """
    Clears the pool document from MongoDB.
    """
    client = get_mongo_client()
    if not client:
        return False
    try:
        db = client["terastream"]
        col = db["settings"]
        col.delete_one({"_id": "session_cookies_pool"})
        return True
    except Exception:
        return False

def get_cookie_pool():
    """
    Reads the stored cookie pool. First checks MongoDB (if active),
    then falls back to local stored_cookies.json file.
    """
    # 1. Try Mongo first
    pool = load_pool_from_mongo()
    if pool is not None:
        return pool

    # 2. Local fallback
    if os.path.exists(STORED_COOKIES_JSON):
        try:
            with open(STORED_COOKIES_JSON, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_cookie_pool(pool):
    """
    Saves the cookie pool. First saves to MongoDB, then backups locally.
    """
    mongo_saved = save_pool_to_mongo(pool)
    try:
        with open(STORED_COOKIES_JSON, "w", encoding="utf-8") as f:
            json.dump(pool, f, indent=2)
    except Exception:
        pass
    return mongo_saved

def migrate_legacy_cookies():
    """
    Migrates legacy single session files (stored_cookies.txt) to the pool format.
    """
    legacy_file = os.path.join(os.path.dirname(__file__), "stored_cookies.txt")
    if os.path.exists(legacy_file) and not os.path.exists(STORED_COOKIES_JSON):
        try:
            with open(legacy_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                # Find ndus in content to make a nice label
                ndus_val = "Imported"
                for item in content.split(";"):
                    if "=" in item:
                        k, v = item.split("=", 1)
                        if k.strip() == "ndus":
                            ndus_val = v.strip()[:6] + "..."
                            break
                            
                pool = [{
                    "id": uuid.uuid4().hex,
                    "cookie_string": content,
                    "blocked_until": 0,
                    "label": f"TeraBox Account ({ndus_val})"
                }]
                save_cookie_pool(pool)
            os.remove(legacy_file)
        except Exception:
            pass

def parse_any_cookie_input(text):
    """
    Parses cookie input from both Netscape cookie file format and standard cookie strings.
    Converts Netscape tab-separated lines into a standard semicolon-separated cookie header.
    """
    text = text.strip()
    if not text:
        return ""
        
    # Check if input text is in Netscape HTTP Cookie File format
    if "Netscape" in text or "\t" in text or (len(text.split('\n')) > 1 and len(re.split(r'\t|\s+', text.split('\n')[0])) >= 5):
        cookies = []
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r'\t|\s+', line)
            if len(parts) >= 7:
                # Name is at index 5, Value is at index 6
                name = parts[5].strip()
                value = parts[6].strip()
                cookies.append(f"{name}={value}")
        return "; ".join(cookies)
    else:
        # Otherwise, assume it is already a semicolon-separated cookie header string
        return text

def extract_surl(url):
    """
    Extracts the surl key from a TeraBox link (supports any domain like 1024terabox, terabox.com, etc.).
    """
    try:
        parsed_url = urllib.parse.urlparse(url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        if "surl" in query_params:
            surl = query_params["surl"][0]
        elif "/s/" in parsed_url.path:
            surl = parsed_url.path.split("/s/")[1].split("/")[0].split("?")[0]
        else:
            surl = parsed_url.path.strip("/")
            
        if surl.startswith("1"):
            surl = surl[1:]
            
        return surl
    except Exception:
        return None

def load_cookies_into_session(session, cookie_input, domain):
    """
    Parses cookie input (single ndus token or complete cookie string) and loads
    it into the session cookie jar for the specified domain.
    """
    if not cookie_input:
        return
    cookie_input = cookie_input.strip()
    if "=" not in cookie_input:
        session.cookies.set("ndus", cookie_input, domain=domain)
        return
        
    pairs = cookie_input.split(";")
    for pair in pairs:
        pair = pair.strip()
        if not pair:
            continue
        if "=" in pair:
            parts = pair.split("=", 1)
            k = parts[0].strip()
            v = parts[1].strip()
            session.cookies.set(k, v, domain=domain)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/docs')
def api_docs():
    return render_template('docs.html')

@app.route('/admin123')
@requires_auth
def admin():
    return render_template('admin.html')

@app.route('/api/admin/cookies', methods=['GET'])
@requires_auth
def get_admin_cookies():
    migrate_legacy_cookies()
    pool = get_cookie_pool()
    
    formatted_pool = []
    now = time.time()
    
    for session in pool:
        cookie_str = session.get("cookie_string", "")
        details = {}
        ndus_val = None
        for item in cookie_str.split(";"):
            if "=" in item:
                k, v = item.split("=", 1)
                k = k.strip()
                v = v.strip()
                details[k] = v
                if k == "ndus":
                    ndus_val = v
                    
        masked_ndus = ndus_val[:4] + "..." + ndus_val[-4:] if ndus_val and len(ndus_val) > 8 else "None"
        
        status = "Active"
        blocked_until = session.get("blocked_until", 0)
        if blocked_until > now:
            status = f"Rate-limited (cooldown: {int(blocked_until - now)}s)"
            
        formatted_pool.append({
            "id": session.get("id"),
            "label": session.get("label", "TeraBox Session"),
            "status": status,
            "masked_ndus": masked_ndus,
            "keys": list(details.keys()),
            "cookies": details,
            "raw_string": cookie_str
        })
        
    source = "MongoDB" if load_pool_from_mongo() is not None else "Local File"
    
    return jsonify({
        "status": "active" if pool else "no_cookies",
        "pool": formatted_pool,
        "source": source
    })

@app.route('/api/admin/cookies', methods=['POST'])
@requires_auth
def save_admin_cookies():
    migrate_legacy_cookies()
    data = request.json
    action = data.get("action", "add")
    pool = get_cookie_pool()
    
    if action == "add":
        raw_text = data.get("cookies", "")
        if not raw_text.strip():
            return jsonify({"error": "Empty cookie input"}), 400
            
        parsed_cookie = parse_any_cookie_input(raw_text)
        if not parsed_cookie:
            return jsonify({"error": "Failed to parse cookie input. Make sure the format is valid."}), 400
            
        # Extract ndus value to name the session
        ndus_val = "Session"
        for item in parsed_cookie.split(";"):
            if "=" in item:
                k, v = item.split("=", 1)
                if k.strip() == "ndus":
                    ndus_val = v.strip()[:6] + "..."
                    break
                    
        new_session = {
            "id": uuid.uuid4().hex,
            "cookie_string": parsed_cookie,
            "blocked_until": 0,
            "label": f"TeraBox Account ({ndus_val})"
        }
        pool.append(new_session)
        save_cookie_pool(pool)
        return jsonify({"success": True})
        
    elif action == "update":
        session_id = data.get("id")
        cookies_dict = data.get("cookies", {})
        
        for s in pool:
            if s.get("id") == session_id:
                # Compile dictionary back to semicolon-separated string
                cookie_str = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
                s["cookie_string"] = cookie_str
                break
        save_cookie_pool(pool)
        return jsonify({"success": True})
        
    elif action == "delete":
        session_id = data.get("id")
        pool = [s for s in pool if s.get("id") != session_id]
        save_cookie_pool(pool)
        return jsonify({"success": True})
        
    return jsonify({"error": "Invalid action"}), 400

@app.route('/api/admin/cookies', methods=['DELETE'])
@requires_auth
def delete_admin_cookies():
    migrate_legacy_cookies()
    save_cookie_pool([])
    delete_cookies_from_mongo()
    return jsonify({"success": True})

@app.route('/api/resolve', methods=['GET'])
def resolve():
    url = request.args.get('url')
    ndus = request.args.get('ndus')
    
    if not url:
        return jsonify({"error": "Missing URL parameter", "errno": -1}), 400
        
    # --- EXTERNAL API PRIMARY RESOLVER ---
    try:
        encoded_url = urllib.parse.quote(url)
        friend_api_url = f"https://tera-download-rose.vercel.app/api/extract?url={encoded_url}"
        f_res = requests.get(friend_api_url, timeout=10)
        if f_res.status_code == 200:
            f_data = f_res.json()
            if f_data.get("success") and "data" in f_data and "streams" in f_data["data"]:
                streams = f_data["data"]["streams"]
                # Grab 480p stream first as requested, fallback to others
                best_stream = streams.get("480p") or streams.get("360p") or streams.get("720p") or streams.get("1080p")
                
                # Crack the Base64 to bypass the Vercel redirect CORS issue
                import base64
                if best_stream and 'api/redirect?data=' in best_stream:
                    try:
                        b64_data = best_stream.split('api/redirect?data=')[1]
                        # Fix padding if missing
                        b64_data += "=" * ((4 - len(b64_data) % 4) % 4)
                        best_stream = base64.b64decode(b64_data).decode('utf-8')
                    except Exception:
                        pass
                
                if best_stream:
                    # Hide the Cloudflare URL by pointing the frontend to our own proxy
                    import urllib.parse
                    proxy_url = f"/api/m3u8?url={urllib.parse.quote(best_stream)}"
                    
                    # Translate JSON structure so the frontend doesn't break
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
    except Exception as e:
        print(f"External API failed, falling back to local scraping: {e}")
    # --- END EXTERNAL API PRIMARY RESOLVER ---
        
    surl = extract_surl(url)
    if not surl:
        return jsonify({"error": "Could not extract share key (surl) from URL", "errno": -1}), 400
        
    # Build list of cookie sessions to try
    now = time.time()
    pool = get_cookie_pool()
    
    if not ndus or ndus.strip() == "":
        if not pool:
            # Fall back to anonymous mode if pool is empty
            sessions_to_try = [None]
        else:
            # Prioritize sessions that are NOT rate-limited
            active_sessions = [s for s in pool if s.get("blocked_until", 0) <= now]
            # If all sessions are rate-limited/in cooldown, try all of them anyway
            sessions_to_try = active_sessions if active_sessions else pool
    else:
        # User entered a manual single cookie in the player input
        sessions_to_try = [{"cookie_string": ndus, "id": "manual"}]
        
    last_error = "No session cookies available."
    last_errno = -1
    
    for session_item in sessions_to_try:
        current_cookie = session_item.get("cookie_string") if session_item else None
        session_id = session_item.get("id") if session_item else None
        
        # Dynamically capture and forward browser headers to match user session exactly
        incoming_ua = request.headers.get("User-Agent", "")
        if not incoming_ua or "python-requests" in incoming_ua.lower() or "urllib" in incoming_ua.lower():
            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        else:
            user_agent = incoming_ua
            
        headers = {
            "User-Agent": user_agent,
            "Accept-Language": request.headers.get("Accept-Language", "en-US,en;q=0.9")
        }
        
        for hint in ["sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"]:
            if hint in request.headers:
                headers[hint] = request.headers[hint]
                
        api_domain = "dm.terabox.app"
        first_url = f"https://{api_domain}/sharing/link?surl={surl}"
        
        try:
            session = requests.Session()
            if current_cookie:
                load_cookies_into_session(session, current_cookie, ".terabox.app")
                
            # Step 1: Get jsToken from sharing/link page
            response = session.get(first_url, headers=headers, timeout=15)
            if response.status_code != 200:
                last_error = f"Failed to load sharing page from TeraBox (HTTP {response.status_code})"
                last_errno = response.status_code
                continue
                
            text = response.text
            match = re.search(r'fn%28%22(.*?)%22%29', text)
            if not match:
                match = re.search(r'fn\("(.*?)"\)', text)
                
            if not match:
                if "need verify" in text or "errno" in text:
                    # Mark this specific session as rate-limited/blocked for 60 seconds
                    if session_id and session_id != "manual":
                        for s in pool:
                            if s.get("id") == session_id:
                                s["blocked_until"] = time.time() + 60
                                break
                        save_cookie_pool(pool)
                    last_error = "TeraBox returned verification check (errno 4000020)."
                    last_errno = 4000020
                    # Try next session in pool!
                    continue
                last_error = "Failed to extract jsToken from TeraBox. Session expired."
                last_errno = -1
                continue
                
            jsToken = match.group(1)
            
            # Step 2: Fetch share/list API
            api_url = f"https://{api_domain}/share/list"
            params = {
                "app_id": "250528",
                "jsToken": jsToken,
                "site_referer": "https://www.terabox.app/",
                "shorturl": surl,
                "root": "1"
            }
            
            api_headers = {
                "Host": api_domain,
                "User-Agent": headers["User-Agent"],
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": headers["Accept-Language"],
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"https://{api_domain}/sharing/link?surl={surl}&clearCache=1",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"https://{api_domain}"
            }
            
            for hint in ["sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"]:
                if hint in request.headers:
                    api_headers[hint] = request.headers[hint]
                    
            api_response = session.get(api_url, params=params, headers=api_headers, timeout=15)
            if api_response.status_code != 200:
                last_error = f"TeraBox API list endpoint returned HTTP {api_response.status_code}"
                last_errno = api_response.status_code
                continue
                
            data = api_response.json()
            
            # Check for API-level verification blocks
            errno = data.get("errno", 0)
            if errno in [4000020, 400141, 400010, 400011, 403]:
                # Mark this specific session as rate-limited/blocked
                if session_id and session_id != "manual":
                    for s in pool:
                        if s.get("id") == session_id:
                            s["blocked_until"] = time.time() + 60
                            break
                    save_cookie_pool(pool)
                last_error = f"TeraBox API returned verification check (errno {errno})."
                last_errno = errno
                # Try next session in pool!
                continue
                
            # Check if session is dead/unauthorized (no download links returned)
            if current_cookie and "list" in data and isinstance(data["list"], list) and len(data["list"]) > 0:
                first_file = data["list"][0]
                if not first_file.get("dlink"):
                    if session_id and session_id != "manual":
                        for s in pool:
                            if s.get("id") == session_id:
                                s["blocked_until"] = time.time() + 60
                                break
                        save_cookie_pool(pool)
                    last_error = "TeraBox account session is expired or invalid (no download links returned)."
                    last_errno = -2
                    # Try next session in pool!
                    continue
                
            # Resolution Success! Mask links using this working session's cookies
            if "list" in data and isinstance(data["list"], list):
                for file_item in data["list"]:
                    raw_dlink = file_item.get("dlink")
                    if raw_dlink:
                        token = uuid.uuid4().hex
                        resolved_links_store[token] = {
                            "dlink": raw_dlink,
                            "ndus": current_cookie,
                            "root_domain": "terabox.app",
                            "filename": file_item.get("server_filename", "video.mp4")
                        }
                        
                        file_item["dlink"] = f"/api/download?id={token}"
                    
                    thumbs = file_item.get("thumbs")
                    if thumbs and isinstance(thumbs, dict):
                        masked_thumbs = {}
                        for key, val in thumbs.items():
                            if val:
                                thumb_token = uuid.uuid4().hex
                                resolved_thumbnails_store[thumb_token] = {
                                    "url": val,
                                    "ndus": current_cookie,
                                    "root_domain": "terabox.app"
                                }
                                masked_thumbs[key] = f"/api/thumbnail?id={thumb_token}"
                        file_item["thumbs"] = masked_thumbs
                        
            return jsonify(data)
            
        except Exception as e:
            last_error = str(e)
            last_errno = -1
            continue
            
    # If all sessions in the pool failed / exhausted
    return jsonify({"error": last_error, "errno": last_errno}), 403

@app.route('/api/download')
def download():
    token = request.args.get('id')
    mode = request.args.get('mode', 'stream')
    
    if not token or token not in resolved_links_store:
        return "File not found or link session has expired.", 404
        
    file_details = resolved_links_store[token]
    dlink = file_details["dlink"]
    ndus = file_details["ndus"]
    root_domain = file_details.get("root_domain", "terabox.app")
    filename = file_details["filename"]
    
    incoming_ua = request.headers.get("User-Agent", "")
    if not incoming_ua or "python-requests" in incoming_ua.lower() or "urllib" in incoming_ua.lower():
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    else:
        user_agent = incoming_ua
        
    headers = {
        "User-Agent": user_agent,
        "Referer": f"https://www.{root_domain}/"
    }
    
    for hint in ["sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "Accept-Language"]:
        if hint in request.headers:
            headers[hint] = request.headers[hint]
            
    if 'Range' in request.headers:
        headers['Range'] = request.headers['Range']
        
    try:
        session = requests.Session()
        if ndus:
            load_cookies_into_session(session, ndus, f".{root_domain}")
            
        r = session.get(dlink, headers=headers, stream=True, timeout=30)
        
        def generate():
            # Bypass requests overhead and read raw bytes directly from the socket
            for chunk in r.raw.stream(262144, decode_content=False):
                if chunk:
                    yield chunk
                
        resp_headers = {}
        for h in ['Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges']:
            if h in r.headers:
                resp_headers[h] = r.headers[h]
                
        if 'Content-Type' not in resp_headers:
            resp_headers['Content-Type'] = 'video/mp4'
            
        if mode == 'attachment':
            resp_headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            
        return Response(generate(), status=r.status_code, headers=resp_headers)
    except Exception as e:
        return str(e), 500

@app.route('/api/thumbnail')
def thumbnail():
    token = request.args.get('id')
    if not token or token not in resolved_thumbnails_store:
        return "Thumbnail not found or has expired.", 404
        
    thumb_details = resolved_thumbnails_store[token]
    url = thumb_details["url"]
    ndus = thumb_details["ndus"]
    root_domain = thumb_details.get("root_domain", "terabox.app")
    
    incoming_ua = request.headers.get("User-Agent", "")
    if not incoming_ua or "python-requests" in incoming_ua.lower() or "urllib" in incoming_ua.lower():
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    else:
        user_agent = incoming_ua
        
    headers = {
        "User-Agent": user_agent,
        "Referer": f"https://www.{root_domain}/"
    }
    
    for hint in ["sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "Accept-Language"]:
        if hint in request.headers:
            headers[hint] = request.headers[hint]
            
    try:
        session = requests.Session()
        if ndus:
            load_cookies_into_session(session, ndus, f".{root_domain}")
            
        r = session.get(url, headers=headers, timeout=15)
        return Response(r.content, status=r.status_code, content_type=r.headers.get('Content-Type', 'image/jpeg'))
    except Exception as e:
        return str(e), 500

@app.route('/api/m3u8', methods=['GET'])
def proxy_m3u8():
    url = request.args.get('url')
    if not url:
        return "Missing URL", 400
    try:
        # Download the tiny .m3u8 file and serve it directly to the frontend
        # This hides the friend's domain from the frontend and the network tab's initial request
        r = requests.get(url, timeout=10)
        return Response(r.text, content_type='application/vnd.apple.mpegurl', headers={
            'Access-Control-Allow-Origin': '*'
        })
    except Exception as e:
        return str(e), 500

if __name__ == '__main__':
    os.makedirs('templates', exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
