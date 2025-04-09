import os
import re
import json
import time
import logging
import hashlib
import requests
import subprocess
import uuid
from flask import Flask, request, jsonify, send_file, make_response
from functools import wraps, lru_cache
import threading

app = Flask(__name__)

# Configuration
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(CURRENT_DIR, "downloads/")
OUTPUT_DIR = os.path.join(CURRENT_DIR, "mp3/")
CACHE_EXPIRY = 86400  # 24 hours (in seconds)
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB max file size

# Create directories if they don't exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('tiktok_to_mp3')

# Cache storage
video_cache = {}  # For TikTok video info
file_cache = {}   # For converted MP3 files
cache_lock = threading.Lock()

# Helper function for logging
def log_message(message):
    if isinstance(message, (dict, list, tuple)):
        logger.info(json.dumps(message))
    else:
        logger.info(message)

# Cache functions for TikTok videos
def get_tiktok_cache(url_hash):
    if url_hash in video_cache and video_cache[url_hash]['expires'] > time.time():
        log_message(f'Cache hit for TikTok: {url_hash}')
        return video_cache[url_hash]['data']
    return False

def set_tiktok_cache(url_hash, data, expiration=CACHE_EXPIRY):
    video_cache[url_hash] = {
        'data': data,
        'expires': time.time() + expiration
    }
    log_message(f'Cache set for TikTok: {url_hash}')
    return True

# Extract TikTok video ID from URL
def extract_tiktok_id(url):
    # Normalize URL
    normalized_url = url
    normalized_url = normalized_url.replace('m.tiktok.com', 'www.tiktok.com')
    normalized_url = normalized_url.replace('vm.tiktok.com', 'www.tiktok.com')
    
    # Regular expressions to match different TikTok URL formats
    patterns = [
        r'tiktok\.com\/@[\w\.]+\/video\/(\d+)',  # Standard format
        r'tiktok\.com\/t\/(\w+)',                # Short URL format
        r'v[mt]\.tiktok\.com\/(\w+)',            # Very short URL format
        r'tiktok\.com\/.*[?&]item_id=(\d+)',     # Query parameter format
    ]
    
    # First try with normalized URL
    for pattern in patterns:
        match = re.search(pattern, normalized_url)
        if match:
            return match.group(1)
    
    # For short URLs - follow redirect
    if 'vm.tiktok.com' in url or 'vt.tiktok.com' in url or len(url.split('/')[2]) < 15:
        return 'follow_redirect'
    
    return None

# Follow redirects to get final URL
def follow_tiktok_redirects(url):
    log_message(f'Following redirects for: {url}')
    
    try:
        response = requests.head(url, allow_redirects=True, 
                               headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'},
                               timeout=10)
        final_url = response.url
        log_message(f'Redirect resolved to: {final_url}')
        return final_url
    except Exception as e:
        log_message(f'Error following redirect: {str(e)}')
        return url

# Try to get TikTok video using TikWM API
def fetch_from_tikwm(url):
    log_message(f'Trying TikWM API service for: {url}')
    
    api_url = 'https://www.tikwm.com/api/'
    
    try:
        response = requests.post(
            api_url,
            data={
                'url': url,
                'hd': 1
            },
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            },
            timeout=30
        )
        
        if response.status_code != 200:
            log_message(f'Error: TikWM API request failed with status: {response.status_code}')
            return None
        
        data = response.json()
        
        if not data.get('data') or data.get('code') != 0:
            log_message(f'TikWM API returned error: {data}')
            return None
        
        video_data = data['data']
        
        return {
            'video_url': video_data['play'],
            'cover_url': video_data['cover'],
            'author': video_data['author']['unique_id'],
            'desc': video_data['title'],
            'video_id': video_data['id'],
            'likes': video_data.get('digg_count', 0),
            'comments': video_data.get('comment_count', 0),
            'shares': video_data.get('share_count', 0),
            'plays': video_data.get('play_count', 0),
            'method': 'tikwm'
        }
    except Exception as e:
        log_message(f'Error using TikWM API: {str(e)}')
        return None

# Try to get TikTok video using SSSTik API
def fetch_from_ssstik(url):
    log_message(f'Trying SSSTik API service for: {url}')
    
    session = requests.Session()
    
    try:
        # First request to get cookies and token
        response = session.get(
            'https://ssstik.io/en',
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            },
            timeout=30
        )
        
        if response.status_code != 200:
            log_message('Failed to access SSSTik service')
            return None
        
        html = response.text
        
        # Extract the tt token
        tt_match = re.search(r'name="tt"[\s]+value="([^"]+)"', html)
        if not tt_match:
            log_message('Failed to extract token from SSSTik')
            return None
        
        tt_token = tt_match.group(1)
        
        # Make the API request
        response = session.post(
            'https://ssstik.io/abc?url=dl',
            data={
                'id': url,
                'locale': 'en',
                'tt': tt_token
            },
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'Origin': 'https://ssstik.io',
                'Referer': 'https://ssstik.io/en',
                'X-Requested-With': 'XMLHttpRequest'
            },
            timeout=30
        )
        
        if response.status_code != 200:
            log_message('Failed to get a response from SSSTik API')
            return None
        
        response_text = response.text
        
        # Parse the HTML response to extract the download link
        video_match = re.search(r'<a href="([^"]+)"[^>]+>Download server 1', response_text)
        if not video_match:
            log_message('Failed to extract download link from SSSTik response')
            return None
        
        video_url = video_match.group(1).replace('&amp;', '&')
        
        # Extract username if available
        author = 'Unknown'
        user_match = re.search(r'<div class="maintext">@([^<]+)</div>', response_text)
        if user_match:
            author = user_match.group(1)
        
        # Extract description/title if available
        desc = ''
        desc_match = re.search(r'<p[^>]+class="maintext">([^<]+)</p>', response_text)
        if desc_match:
            desc = desc_match.group(1)
        
        return {
            'video_url': video_url,
            'author': author,
            'desc': desc,
            'video_id': hashlib.md5(url.encode()).hexdigest(),
            'cover_url': '',
            'likes': 0,
            'comments': 0,
            'shares': 0,
            'plays': 0,
            'method': 'ssstik'
        }
    except Exception as e:
        log_message(f'Error using SSSTik service: {str(e)}')
        return None

# Functions for MP3 conversion
def sanitize_filename(name):
    """Remove any path info and sanitize the file name"""
    name = os.path.basename(name)
    name = name.replace(' ', '_')
    name = re.sub(r'[^A-Za-z0-9_\-\.]', '', name)
    return name

def generate_unique_filename(original_name):
    """Generate a unique filename based on the original name"""
    filename, extension = os.path.splitext(original_name)
    unique_id = uuid.uuid4().hex[:10]
    return f"{sanitize_filename(filename)}_{unique_id}{extension}"

@lru_cache(maxsize=10)
def get_ffmpeg_version():
    """Cache the FFmpeg version to avoid repeated subprocess calls"""
    try:
        process = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        return process.stdout.split('\n')[0]
    except Exception as e:
        return f"FFmpeg version check failed: {str(e)}"

def cleanup_expired_files():
    """Remove files that haven't been accessed for CACHE_EXPIRY seconds"""
    current_time = time.time()
    with cache_lock:
        # Clean video cache
        expired_keys = []
        for key, data in video_cache.items():
            if current_time > data["expires"]:
                expired_keys.append(key)
        
        for key in expired_keys:
            del video_cache[key]
        
        # Clean MP3 file cache
        expired_keys = []
        for key, data in file_cache.items():
            if current_time - data["last_accessed"] > CACHE_EXPIRY:
                try:
                    if os.path.exists(data["output_path"]):
                        os.remove(data["output_path"])
                        logger.info(f"Removed expired file: {data['output_path']}")
                    expired_keys.append(key)
                except Exception as e:
                    logger.error(f"Error removing file {data['output_path']}: {str(e)}")
        
        for key in expired_keys:
            del file_cache[key]

def start_cleanup_thread():
    """Start a background thread to periodically clean up expired files"""
    def cleanup_task():
        while True:
            cleanup_expired_files()
            time.sleep(300)  # Run every 5 minutes

    cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()

def download_tiktok_video(url):
    """Download a TikTok video and return the local file path"""
    # Normalize URL for short links
    if 'vm.tiktok.com' in url or 'vt.tiktok.com' in url or len(url.split('/')[2]) < 15:
        url = follow_tiktok_redirects(url)
    
    # Try to get video info
    result = fetch_from_tikwm(url)
    if not result:
        result = fetch_from_ssstik(url)
    
    if not result or not result.get('video_url'):
        raise Exception("Failed to extract video URL from TikTok link")
    
    video_url = result['video_url']
    author = result['author']
    
    # Generate a nice filename based on the author and a unique ID
    filename = f"{sanitize_filename(author)}_{uuid.uuid4().hex[:8]}.mp4"
    file_path = os.path.join(UPLOAD_DIR, filename)
    
    # Download the video
    log_message(f"Downloading video from {video_url} to {file_path}")
    response = requests.get(video_url, stream=True, timeout=30)
    
    if response.status_code != 200:
        raise Exception(f"Failed to download video: HTTP {response.status_code}")
    
    with open(file_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    
    return {
        'file_path': file_path,
        'author': author,
        'desc': result['desc'],
        'video_id': result['video_id'],
        'filename': filename
    }

def convert_to_mp3(video_path, author, desc):
    """Convert video to MP3 and return the MP3 file path"""
    # Generate output filename
    output_filename = f"{os.path.splitext(os.path.basename(video_path))[0]}.mp3"
    output_path = os.path.join(OUTPUT_DIR, output_filename)
    
    # Convert the video to MP3 using FFmpeg
    log_message(f"Converting {video_path} to MP3: {output_path}")
    
    ffmpeg_command = [
        "ffmpeg", 
        "-i", video_path, 
        "-vn",  # No video
        "-ar", "44100",  # Audio sample rate
        "-ac", "2",  # Stereo
        "-b:a", "192k",  # Bitrate
        "-metadata", f"artist={author}",  # Set artist metadata
        "-metadata", f"title={desc[:30]}",  # Set title metadata (truncated to 30 chars)
        "-threads", "0",  # Use all available threads
        "-preset", "fast",  # Use faster preset
        output_path
    ]
    
    process = subprocess.run(
        ffmpeg_command, 
        stdout=subprocess.PIPE, 
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Check if conversion was successful
    if process.returncode != 0:
        raise Exception(f"FFmpeg conversion failed: {process.stderr}")
    
    # Check if output file exists
    if not os.path.exists(output_path):
        raise Exception("Output file was not created")
    
    # Delete the original video file
    try:
        os.remove(video_path)
    except Exception as e:
        log_message(f"Warning: Could not delete original video file: {str(e)}")
    
    return output_path

# CORS middleware
def cors_middleware(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        response = make_response(f(*args, **kwargs))
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
        response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
        return response
    return decorated_function

# Routes
@app.route('/api/tiktok-to-mp3', methods=['POST', 'OPTIONS'])
@cors_middleware
def tiktok_to_mp3():
    """Endpoint that takes a TikTok URL and returns an MP3 download link"""
    # Handle CORS preflight request
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    
    if not request.is_json:
        return jsonify({'success': False, 'error': 'Request must be in JSON format'}), 400
    
    data = request.get_json()
    
    if not data.get('url'):
        return jsonify({'success': False, 'error': 'TikTok URL is required'}), 400
    
    tiktok_url = data['url'].strip()
    url_hash = hashlib.md5(tiktok_url.encode()).hexdigest()
    
    # Check for cached result
    cached_result = get_tiktok_cache(url_hash)
    if cached_result:
        return jsonify(cached_result)
    
    try:
        # 1. Download the TikTok video
        video_info = download_tiktok_video(tiktok_url)
        video_path = video_info['file_path']
        
        # 2. Convert video to MP3
        mp3_path = convert_to_mp3(video_path, video_info['author'], video_info['desc'])
        mp3_filename = os.path.basename(mp3_path)
        
        # 3. Add to cache
        file_hash = hashlib.md5(tiktok_url.encode()).hexdigest()
        with cache_lock:
            file_cache[file_hash] = {
                "output_path": mp3_path,
                "last_accessed": time.time()
            }
        
        # 4. Create result
        result = {
            'success': True,
            'mp3_url': f"/download/{mp3_filename}",
            'filename': mp3_filename,
            'author': video_info['author'],
            'title': video_info['desc'],
            'cached': False
        }
        
        # Add to cache
        set_tiktok_cache(url_hash, result)
        
        return jsonify(result)
    
    except Exception as e:
        log_message(f"Error processing TikTok to MP3: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    """Serve the converted MP3 files"""
    file_path = os.path.join(OUTPUT_DIR, filename)
    
    # Update last accessed time in cache
    with cache_lock:
        for file_hash, data in file_cache.items():
            if os.path.basename(data["output_path"]) == filename:
                file_cache[file_hash]["last_accessed"] = time.time()
                break
    
    if os.path.exists(file_path):
        return send_file(
            file_path, 
            as_attachment=True,
            download_name=filename,
            mimetype='audio/mpeg'
        )
    else:
        return jsonify({'success': False, 'error': f"File not found: {filename}"}), 404

@app.route('/status', methods=['GET'])
def status():
    """Status endpoint for health checks"""
    with cache_lock:
        video_cache_count = len(video_cache)
        file_cache_count = len(file_cache)
    
    return jsonify({
        'status': 'running',
        'ffmpeg_version': get_ffmpeg_version(),
        'video_cache_count': video_cache_count,
        'file_cache_count': file_cache_count,
        'cache_expiry_seconds': CACHE_EXPIRY
    })

@app.route('/clear-cache', methods=['POST'])
def clear_cache():
    """Admin endpoint to manually clear all caches"""
    try:
        with cache_lock:
            global video_cache, file_cache
            video_cache = {}
            
            # Remove MP3 files in file_cache
            for data in file_cache.values():
                try:
                    if os.path.exists(data["output_path"]):
                        os.remove(data["output_path"])
                except Exception as e:
                    log_message(f"Error removing file: {str(e)}")
            
            file_cache = {}
        
        return jsonify({'success': True, 'message': 'All caches cleared successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# Example command line usage
def tiktok_to_mp3_cli(tiktok_url, output_path=None):
    """Command line function to download a TikTok video and convert it to MP3"""
    try:
        print(f"Downloading TikTok video: {tiktok_url}")
        video_info = download_tiktok_video(tiktok_url)
        video_path = video_info['file_path']
        
        print(f"Converting to MP3...")
        mp3_path = convert_to_mp3(video_path, video_info['author'], video_info['desc'])
        
        # If output path is specified, copy the file there
        if output_path:
            if not os.path.isdir(output_path):
                os.makedirs(output_path, exist_ok=True)
            
            import shutil
            dest_path = os.path.join(output_path, os.path.basename(mp3_path))
            shutil.copy2(mp3_path, dest_path)
            mp3_path = dest_path
        
        print(f"\nSuccess! MP3 saved to: {mp3_path}")
        return mp3_path
    
    except Exception as e:
        print(f"Error: {str(e)}")
        return None

if __name__ == '__main__':
    import sys
    
    # If run directly, check if command line arguments are provided
    if len(sys.argv) > 1:
        url = sys.argv[1]
        output_dir = sys.argv[2] if len(sys.argv) > 2 else None
        tiktok_to_mp3_cli(url, output_dir)
    else:
        # Start the cleanup thread
        start_cleanup_thread()
        logger.info("Started cache cleanup thread")
        
        # Start the Flask app
        print("Starting TikTok to MP3 Converter API...")
        print("Usage examples:")
        print("  - API: POST to /api/tiktok-to-mp3 with JSON payload {\"url\": \"https://www.tiktok.com/@username/video/1234567890\"}")
        print("  - CLI: python tiktok_to_mp3.py https://www.tiktok.com/@username/video/1234567890 [output_directory]")
        app.run(host='0.0.0.0', port=5000, debug=False)
