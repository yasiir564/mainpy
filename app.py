import os
import re
import json
import time
import logging
import hashlib
import requests
import subprocess
import uuid
from flask import Flask, request, jsonify, send_file
from functools import wraps, lru_cache
import threading

app = Flask(__name__)

# Configuration
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(CURRENT_DIR, "downloads/")
OUTPUT_DIR = os.path.join(CURRENT_DIR, "mp3/")
CACHE_EXPIRY = 3600  # 1 hour (in seconds)
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB max file size

# Create directories if they don't exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
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
        return video_cache[url_hash]['data']
    return False

def set_tiktok_cache(url_hash, data, expiration=CACHE_EXPIRY):
    video_cache[url_hash] = {
        'data': data,
        'expires': time.time() + expiration
    }
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

# Primary method: TikWM API
def fetch_from_tikwm(url):
    log_message(f'Trying TikWM API for: {url}')
    
    api_url = 'https://www.tikwm.com/api/'
    
    try:
        response = requests.post(
            api_url,
            data={'url': url, 'hd': 0},  # Lower quality to save resources
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
            timeout=15
        )
        
        if response.status_code != 200:
            log_message(f'TikWM API request failed with status: {response.status_code}')
            return None
        
        data = response.json()
        
        if not data.get('data') or data.get('code') != 0:
            log_message(f'TikWM API returned error: {data}')
            return None
        
        video_data = data['data']
        
        return {
            'video_url': video_data['play'],
            'author': video_data['author']['unique_id'] if video_data.get('author') else 'tiktok_user',
            'video_id': video_data['id'],
            'method': 'tikwm'
        }
    except Exception as e:
        log_message(f'Error using TikWM API: {str(e)}')
        return None

# Simple fallback method - direct player URL extraction
def fetch_direct_url(url):
    log_message(f'Trying direct extraction for: {url}')
    
    try:
        # Try to get the page content
        response = requests.get(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            },
            timeout=15
        )
        
        if response.status_code != 200:
            return None
            
        # Look for the video URL in the HTML
        html_content = response.text
        
        # Extract username if available
        author_match = re.search(r'uniqueId\\":\\"([^"\\]+)', html_content)
        author = author_match.group(1) if author_match else 'tiktok_user'
        
        # Try to find video URL
        video_url_match = re.search(r'playAddr\\":\\"([^"\\]+)', html_content)
        if not video_url_match:
            video_url_match = re.search(r'"playAddr":"([^"]+)', html_content)
        
        if not video_url_match:
            return None
            
        video_url = video_url_match.group(1).replace('\\u002F', '/').replace('\\', '')
        
        # If URL doesn't start with http, add it
        if not video_url.startswith('http'):
            video_url = 'https:' + video_url
            
        return {
            'video_url': video_url,
            'author': author,
            'video_id': hashlib.md5(url.encode()).hexdigest()[:16],
            'method': 'direct'
        }
    except Exception as e:
        log_message(f'Error using direct extraction: {str(e)}')
        return None

# Functions for MP3 conversion
def sanitize_filename(name):
    """Remove any path info and sanitize the file name"""
    name = os.path.basename(name)
    name = name.replace(' ', '_')
    name = re.sub(r'[^A-Za-z0-9_\-\.]', '', name)
    return name[:50]  # Limit filename length

def generate_unique_filename(original_name):
    """Generate a unique filename based on the original name"""
    filename, extension = os.path.splitext(original_name)
    unique_id = uuid.uuid4().hex[:6]
    return f"{sanitize_filename(filename)}_{unique_id}{extension}"

def cleanup_expired_files():
    """Remove files that haven't been accessed for CACHE_EXPIRY seconds"""
    current_time = time.time()
    with cache_lock:
        # Clean video cache
        expired_keys = [key for key, data in video_cache.items() if current_time > data["expires"]]
        for key in expired_keys:
            del video_cache[key]
        
        # Clean MP3 file cache
        expired_keys = []
        for key, data in file_cache.items():
            if current_time - data["last_accessed"] > CACHE_EXPIRY:
                try:
                    if os.path.exists(data["output_path"]):
                        os.remove(data["output_path"])
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
            time.sleep(600)  # Run every 10 minutes

    cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()

def download_tiktok_video(url):
    """Download a TikTok video and return the local file path"""
    # Normalize URL for short links
    if 'vm.tiktok.com' in url or 'vt.tiktok.com' in url:
        url = follow_tiktok_redirects(url)
    
    # Try primary method first
    result = fetch_from_tikwm(url)
    
    # If primary fails, try fallback method
    if not result or not result.get('video_url'):
        result = fetch_direct_url(url)
    
    if not result or not result.get('video_url'):
        raise Exception("Failed to extract video URL from TikTok link")
    
    video_url = result['video_url']
    author = result['author']
    
    # Generate a filename based on the author and a unique ID
    filename = f"{sanitize_filename(author)}_{uuid.uuid4().hex[:6]}.mp4"
    file_path = os.path.join(UPLOAD_DIR, filename)
    
    # Download the video with limited buffer size
    try:
        response = requests.get(video_url, stream=True, timeout=20, 
                              headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        
        if response.status_code != 200:
            raise Exception(f"Failed to download video: HTTP {response.status_code}")
        
        file_size = 0
        with open(file_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=4096):
                if chunk:
                    file_size += len(chunk)
                    if file_size > MAX_FILE_SIZE:
                        f.close()
                        os.remove(file_path)
                        raise Exception("File too large")
                    f.write(chunk)
    except Exception as e:
        # Clean up file if download fails
        if os.path.exists(file_path):
            os.remove(file_path)
        raise Exception(f"Download failed: {str(e)}")
    
    return {
        'file_path': file_path,
        'author': author,
        'video_id': result['video_id'],
        'filename': filename
    }

def convert_to_mp3(video_path, author, quality="medium"):
    """Convert video to MP3 with configurable quality and return the MP3 file path"""
    # Set bitrate based on quality
    bitrates = {
        "low": "64k",
        "medium": "128k",
        "high": "192k"
    }
    bitrate = bitrates.get(quality, "128k")
    
    # Generate output filename
    output_filename = f"{os.path.splitext(os.path.basename(video_path))[0]}.mp3"
    output_path = os.path.join(OUTPUT_DIR, output_filename)
    
    try:
        # Check if FFmpeg is available
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        
        # Convert the video to MP3 using FFmpeg with optimized settings
        ffmpeg_command = [
            "ffmpeg", 
            "-i", video_path,
            "-vn",  # No video
            "-ar", "44100",  # Audio sample rate
            "-ac", "2",  # Stereo
            "-b:a", bitrate,  # Configurable bitrate
            "-metadata", f"artist={author[:30]}",  # Set artist metadata
            "-f", "mp3",  # Force MP3 format
            "-threads", "2",  # Limit threads to save resources
            "-loglevel", "error",  # Reduce logging
            "-y",  # Overwrite output files
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
        
        # Check if output file exists and has size
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise Exception("Output file was not created or is empty")
        
    except Exception as e:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except:
                pass
        raise e
    finally:
        # Delete the original video file to free up space
        try:
            if os.path.exists(video_path):
                os.remove(video_path)
        except Exception as e:
            logger.warning(f"Could not delete original video file: {str(e)}")
    
    return output_path

# Apply CORS to all routes
@app.after_request
def add_cors_headers(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# Special handling for OPTIONS requests
@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def handle_options(path):
    return '', 200

# Root route handler
@app.route('/', methods=['GET', 'POST'])
def root():
    if request.method == 'POST':
        return tiktok_to_mp3()
    return jsonify({'status': 'running', 'message': 'TikTok to MP3 API is running'})

# Routes
@app.route('/api/tiktok-to-mp3', methods=['POST', 'OPTIONS'])
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
    quality = data.get('quality', 'medium')  # Default to medium quality
    
    # Validate quality parameter
    if quality not in ['low', 'medium', 'high']:
        quality = 'medium'
        
    url_hash = hashlib.md5((tiktok_url + quality).encode()).hexdigest()
    
    # Check for cached result
    cached_result = get_tiktok_cache(url_hash)
    if cached_result:
        return jsonify(cached_result)
    
    try:
        # 1. Download the TikTok video
        video_info = download_tiktok_video(tiktok_url)
        video_path = video_info['file_path']
        
        # 2. Convert video to MP3
        mp3_path = convert_to_mp3(video_path, video_info['author'], quality)
        mp3_filename = os.path.basename(mp3_path)
        
        # 3. Add to cache
        file_hash = hashlib.md5((tiktok_url + quality).encode()).hexdigest()
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
            'quality': quality,
            'cached': False
        }
        
        # Add to cache
        set_tiktok_cache(url_hash, result)
        
        return jsonify(result)
    
    except Exception as e:
        logger.error(f"Error processing TikTok to MP3: {str(e)}")
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
    
    # Check if FFmpeg is available
    ffmpeg_status = "available"
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except Exception:
        ffmpeg_status = "not available"
    
    return jsonify({
        'status': 'running',
        'ffmpeg': ffmpeg_status,
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
                    logger.warning(f"Error removing file: {str(e)}")
            
            file_cache = {}
        
        return jsonify({'success': True, 'message': 'All caches cleared successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    import sys
    
    # Start the cleanup thread
    start_cleanup_thread()
    logger.info("Started cache cleanup thread")
    
    # Start the Flask app with minimal settings
    print("Starting TikTok to MP3 Converter API...")
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
