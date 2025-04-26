from flask import Flask, request, jsonify, send_file, make_response
from flask_cors import CORS
import requests
import re
import os
import tempfile
import logging
import time
import random
import json
import hashlib
from urllib.parse import urlparse
import subprocess
import shutil
from functools import lru_cache
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Configure CORS with more specific settings
CORS(app, resources={r"/api/*": {"origins": "*", "methods": ["GET", "POST", "OPTIONS"], 
                                "allow_headers": ["Content-Type", "Authorization"]}})

# Set up logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Directory for caching downloaded videos and audio
CACHE_DIR = os.environ.get('CACHE_DIR', os.path.join(tempfile.gettempdir(), 'tiktok_cache'))
os.makedirs(CACHE_DIR, exist_ok=True)

# Check if ffmpeg is installed
try:
    ffmpeg_path = shutil.which('ffmpeg')
    if not ffmpeg_path:
        logger.warning("ffmpeg not found in PATH. Audio conversion will not work.")
    else:
        logger.info(f"ffmpeg found at: {ffmpeg_path}")
except Exception as e:
    logger.error(f"Error checking for ffmpeg: {e}")
    ffmpeg_path = None

# Audio quality options
AUDIO_QUALITY = {
    "low": {"bitrate": "64k", "sample_rate": "22050"},
    "medium": {"bitrate": "128k", "sample_rate": "44100"},
    "high": {"bitrate": "192k", "sample_rate": "48000"}
}

# Default audio quality
DEFAULT_AUDIO_QUALITY = "medium"

# Cache configuration - max items to keep in memory
MAX_CACHE_ITEMS = 100

# Expanded list of user agents to rotate
USER_AGENTS = [
    # Mobile User Agents
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_8_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/101.0.4951.44 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; SM-A515F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.104 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.41 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.41 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; Redmi Note 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPad; CPU OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    
    # Desktop User Agents
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.67 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.54 Safari/537.36 Edg/101.0.1210.39",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:100.0) Gecko/20100101 Firefox/100.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_3_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.64 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.64 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:100.0) Gecko/20100101 Firefox/100.0",
    
    # Tablet User Agents
    "Mozilla/5.0 (iPad; CPU OS 15_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 12; SM-X906C Build/QP1A.190711.020) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.41 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; Lenovo TB-X606F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.41 Safari/537.36"
]

def get_random_user_agent():
    return random.choice(USER_AGENTS)

def get_cache_key(url, is_audio=False, quality=DEFAULT_AUDIO_QUALITY):
    """Generate a cache key based on URL and options."""
    key_string = f"{url}|{is_audio}|{quality}" if is_audio else url
    return hashlib.md5(key_string.encode('utf-8')).hexdigest()

def get_cache_path(cache_key, is_audio=False):
    """Get the file path for a cached item."""
    extension = "mp3" if is_audio else "mp4"
    return os.path.join(CACHE_DIR, f"{cache_key}.{extension}")

def is_in_cache(cache_key, is_audio=False):
    """Check if an item exists in cache and is valid."""
    file_path = get_cache_path(cache_key, is_audio)
    if os.path.exists(file_path) and os.path.getsize(file_path) > 10000:
        # Check if the file is not older than 24 hours
        if time.time() - os.path.getmtime(file_path) < 86400:  # 24 hours
            return True
    return False

def cleanup_cache():
    """Remove old files if cache directory gets too large."""
    try:
        files = os.listdir(CACHE_DIR)
        if len(files) > MAX_CACHE_ITEMS:
            files_with_time = [(f, os.path.getmtime(os.path.join(CACHE_DIR, f))) for f in files]
            files_with_time.sort(key=lambda x: x[1])  # Sort by modification time
            
            # Remove the oldest files
            for file_name, _ in files_with_time[:len(files_with_time) - MAX_CACHE_ITEMS]:
                try:
                    os.remove(os.path.join(CACHE_DIR, file_name))
                    logger.info(f"Removed old cache file: {file_name}")
                except Exception as e:
                    logger.error(f"Error removing cache file {file_name}: {e}")
    except Exception as e:
        logger.error(f"Error cleaning up cache: {e}")

def is_valid_tiktok_url(url):
    """Check if the URL is a valid TikTok URL."""
    parsed_url = urlparse(url)
    return parsed_url.netloc in ["www.tiktok.com", "tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "m.tiktok.com"]

def expand_shortened_url(url):
    """Expand a shortened TikTok URL."""
    try:
        headers = {"User-Agent": get_random_user_agent()}
        response = requests.head(url, allow_redirects=True, timeout=10, headers=headers)
        return response.url
    except Exception as e:
        logger.error(f"Error expanding shortened URL: {e}")
        return url

def extract_video_id(url):
    """Extract the video ID from a TikTok URL."""
    # Handle shortened URLs
    if "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        url = expand_shortened_url(url)
    
    # Extract video ID from URL
    patterns = [
        r'/video/(\d+)',
        r'tiktok\.com\/@[\w.-]+/video/(\d+)',
        r'v/(\d+)'  # For shortened URLs that redirect to /v/{id}
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None

def download_tiktok_video_mobile(video_id, remove_watermark=False):
    """Download TikTok video using the mobile website."""
    try:
        # Direct video URL
        mobile_url = f"https://m.tiktok.com/v/{video_id}"
        
        headers = {
            "User-Agent": get_random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        }
        
        logger.info(f"Fetching mobile TikTok page: {mobile_url}")
        response = requests.get(mobile_url, headers=headers, timeout=20)
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch mobile TikTok page. Status: {response.status_code}")
            return None
        
        # Patterns to extract video URLs
        video_patterns = [
            # Look for playAddr (no watermark) or downloadAddr (with watermark)
            r'"playAddr":"([^"]+)"' if remove_watermark else r'"downloadAddr":"([^"]+)"',
            r'"playAddr":"([^"]+)"',  # Fallback to playAddr
            r'"downloadAddr":"([^"]+)"',  # Fallback to downloadAddr
            r'"playUrl":"([^"]+)"',  # Alternative format
            r'"videoUrl":"([^"]+)"',  # Alternative format
            # Just look for any mp4 URL
            r'(https://[^"\']+\.mp4[^"\'\s]*)'
        ]
        
        video_url = None
        for pattern in video_patterns:
            matches = re.findall(pattern, response.text)
            if matches:
                # Use the first match
                video_url = matches[0]
                video_url = video_url.replace('\\u002F', '/').replace('\\', '')
                logger.info(f"Found video URL: {video_url[:60]}...")
                break
        
        if not video_url:
            logger.error("No video URL found in the page.")
            return None
        
        # Download the video
        logger.info(f"Downloading video from URL")
        video_headers = {
            "User-Agent": get_random_user_agent(),
            "Referer": mobile_url,
            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.5",
            "Range": "bytes=0-",  # Request the full file
            "DNT": "1",
            "Connection": "keep-alive"
        }
        
        video_response = requests.get(video_url, headers=video_headers, stream=True, timeout=30)
        
        if video_response.status_code not in [200, 206]:  # 206 for partial content
            logger.error(f"Failed to download video. Status: {video_response.status_code}")
            return None
        
        # Create a temporary file
        cache_file = os.path.join(CACHE_DIR, f"{video_id}.mp4")
        
        # Stream the video to the file
        total_size = 0
        with open(cache_file, 'wb') as f:
            for chunk in video_response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    total_size += len(chunk)
        
        logger.info(f"Downloaded video file size: {total_size} bytes")
        
        if total_size < 10000:  # If file is too small, likely an error
            logger.error(f"Downloaded file is too small: {total_size} bytes")
            os.remove(cache_file)
            return None
            
        return cache_file
        
    except Exception as e:
        logger.error(f"Error downloading video: {e}")
        return None

def download_tiktok_video_web(video_id, remove_watermark=False):
    """Alternative method using web API."""
    try:
        # Build the web URL
        web_url = f"https://www.tiktok.com/api/item/detail/?itemId={video_id}"
        
        headers = {
            "User-Agent": get_random_user_agent(),
            "Referer": f"https://www.tiktok.com/video/{video_id}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": "tt_csrf_token=xxxx; tt_webid=xxxx"  # Using dummy values
        }
        
        logger.info(f"Fetching TikTok web API: {web_url}")
        response = requests.get(web_url, headers=headers, timeout=20)
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch TikTok web API. Status: {response.status_code}")
            return None
        
        try:
            data = response.json()
            logger.info("Successfully parsed API response")
            
            # Navigate the JSON structure to find the video URL
            if "itemInfo" in data and "itemStruct" in data["itemInfo"]:
                video_data = data["itemInfo"]["itemStruct"]["video"]
                
                if remove_watermark and "playAddr" in video_data:
                    video_url = video_data["playAddr"]
                elif "downloadAddr" in video_data:
                    video_url = video_data["downloadAddr"]
                else:
                    logger.error("Could not find video URL in API response")
                    return None
                
                # Download the video
                logger.info(f"Downloading video from API URL")
                video_headers = {
                    "User-Agent": get_random_user_agent(),
                    "Referer": f"https://www.tiktok.com/video/{video_id}",
                    "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
                    "Accept-Language": "en-US,en;q=0.9"
                }
                
                video_response = requests.get(video_url, headers=video_headers, stream=True, timeout=30)
                
                if video_response.status_code != 200:
                    logger.error(f"Failed to download video from API. Status: {video_response.status_code}")
                    return None
                
                # Create a cache file
                cache_file = os.path.join(CACHE_DIR, f"{video_id}.mp4")
                
                # Stream the video to the file
                with open(cache_file, 'wb') as f:
                    for chunk in video_response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                
                if os.path.getsize(cache_file) < 10000:  # If file is too small, likely an error
                    logger.error(f"Downloaded file is too small: {os.path.getsize(cache_file)} bytes")
                    os.remove(cache_file)
                    return None
                    
                return cache_file
            else:
                logger.error("Unexpected API response structure")
                return None
                
        except json.JSONDecodeError:
            logger.error("Failed to parse API response as JSON")
            return None
            
    except Exception as e:
        logger.error(f"Error in web API method: {e}")
        return None

def download_tiktok_video_embed(video_id, remove_watermark=False):
    """Try downloading via TikTok's embed functionality."""
    try:
        # Build the embed URL
        embed_url = f"https://www.tiktok.com/embed/v2/{video_id}"
        
        headers = {
            "User-Agent": get_random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
        }
        
        logger.info(f"Fetching TikTok embed page: {embed_url}")
        response = requests.get(embed_url, headers=headers, timeout=20)
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch TikTok embed page. Status: {response.status_code}")
            return None
        
        # Look for video URL in the embed page
        video_patterns = [
            r'<video[^>]+src="([^"]+)"',
            r'"contentUrl":"([^"]+)"',
            r'"playAddr":"([^"]+)"',
            r'"url":"([^"]+\.mp4[^"]*)"'
        ]
        
        video_url = None
        for pattern in video_patterns:
            matches = re.findall(pattern, response.text)
            if matches:
                video_url = matches[0]
                video_url = video_url.replace('\\u002F', '/').replace('\\', '')
                logger.info(f"Found video URL in embed: {video_url[:60]}...")
                break
        
        if not video_url:
            logger.error("No video URL found in the embed page.")
            return None
        
        # Download the video
        logger.info(f"Downloading video from embed URL")
        video_headers = {
            "User-Agent": get_random_user_agent(),
            "Referer": embed_url,
            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.5"
        }
        
        video_response = requests.get(video_url, headers=video_headers, stream=True, timeout=30)
        
        if video_response.status_code != 200:
            logger.error(f"Failed to download video from embed. Status: {video_response.status_code}")
            return None
        
        # Create a temporary file
        cache_file = os.path.join(CACHE_DIR, f"{video_id}.mp4")
        
        # Stream the video to the file
        with open(cache_file, 'wb') as f:
            for chunk in video_response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        if os.path.getsize(cache_file) < 10000:  # If file is too small, likely an error
            logger.error(f"Downloaded file is too small: {os.path.getsize(cache_file)} bytes")
            os.remove(cache_file)
            return None
            
        return cache_file
        
    except Exception as e:
        logger.error(f"Error in embed method: {e}")
        return None

@lru_cache(maxsize=100)
def get_tiktok_video(url, remove_watermark=False):
    """Try multiple methods to download TikTok video with caching."""
    # Extract video ID from URL
    video_id = extract_video_id(url)
    if not video_id:
        logger.error(f"Could not extract video ID from URL: {url}")
        return None, None
    
    logger.info(f"Extracted video ID: {video_id}")
    
    # Check if the video is already cached
    cache_key = get_cache_key(url)
    cache_path = get_cache_path(cache_key)
    
    if is_in_cache(cache_key):
        logger.info(f"Found video in cache: {cache_path}")
        return cache_path, video_id
    
    # List of methods to try in order
    methods = [
        (download_tiktok_video_mobile, (video_id, remove_watermark)),
        (download_tiktok_video_web, (video_id, remove_watermark)),
        (download_tiktok_video_embed, (video_id, remove_watermark))
    ]
    
    for method, args in methods:
        try:
            logger.info(f"Trying download method: {method.__name__}")
            result = method(*args)
            if result:
                logger.info(f"Successfully downloaded video using {method.__name__}")
                
                # Copy to cache if it's not already there
                if result != cache_path:
                    shutil.copy(result, cache_path)
                    logger.info(f"Video cached at: {cache_path}")
                
                # Clean up cache if necessary
                cleanup_cache()
                
                return cache_path, video_id
                
            # Wait a moment before trying the next method to avoid rate limiting
            time.sleep(1)
        except Exception as e:
            logger.error(f"Error in download method {method.__name__}: {e}")
    
    logger.error("All download methods failed")
    return None, video_id

def convert_to_audio(video_path, video_id, quality=DEFAULT_AUDIO_QUALITY):
    """Convert video file to MP3 audio using ffmpeg."""
    if not ffmpeg_path:
        logger.error("ffmpeg not found, cannot convert to audio")
        return None
    
    try:
        # Create a unique cache key for this audio conversion
        cache_key = get_cache_key(video_path, is_audio=True, quality=quality)
        audio_path = get_cache_path(cache_key, is_audio=True)
        
        # Check if audio is already cached
        if is_in_cache(cache_key, is_audio=True):
            logger.info(f"Found audio in cache: {audio_path}")
            return audio_path
        
        # Get quality settings
        quality_settings = AUDIO_QUALITY.get(quality, AUDIO_QUALITY[DEFAULT_AUDIO_QUALITY])
        bitrate = quality_settings["bitrate"]
        sample_rate = quality_settings["sample_rate"]
        
        logger.info(f"Converting video to audio with quality: {quality} (bitrate: {bitrate}, sample rate: {sample_rate})")
        
        # Run ffmpeg to convert the video to audio
        command = [
            ffmpeg_path,
            "-i", video_path,
            "-vn",  # No video
            "-ar", sample_rate,  # Audio sampling rate
            "-ab", bitrate,  # Audio bitrate
            "-f", "mp3",  # Output format
            audio_path
        ]
        
        # Execute the command
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = process.communicate()
        
        if process.returncode != 0:
            logger.error(f"Error converting video to audio: {stderr.decode()}")
            return None
        
        logger.info(f"Successfully converted video to audio: {audio_path}")
        return audio_path
        
    except Exception as e:
        logger.error(f"Error in audio conversion: {e}")
        return None

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint to verify service is running."""
    ffmpeg_status = "available" if ffmpeg_path else "not available"
    return jsonify({
        "status": "ok", 
        "message": "TikTok downloader service is running",
        "ffmpeg": ffmpeg_status,
        "cache_items": len(os.listdir(CACHE_DIR)) if os.path.exists(CACHE_DIR) else 0
    })

@app.route('/api/download', methods=['POST', 'OPTIONS'])
def download_video():
    """API endpoint to download TikTok videos."""
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()
    
    try:
        data = request.json
        if not data:
            return jsonify({"error": "Invalid JSON data"}), 400
            
        url = data.get('url')
        remove_watermark = data.get('remove_watermark', False)
        format_type = data.get('format', 'video').lower()  # 'video' or 'audio'
        audio_quality = data.get('quality', DEFAULT_AUDIO_QUALITY).lower()
        
        # Validate audio quality
        if format_type == 'audio' and audio_quality not in AUDIO_QUALITY:
            audio_quality = DEFAULT_AUDIO_QUALITY
        
        if not url:
            return jsonify({"error": "No URL provided"}), 400
        
        if not is_valid_tiktok_url(url):
            return jsonify({"error": "Invalid TikTok URL"}), 400
        
        # Debug log
        logger.info(f"Processing download request for URL: {url}")
        logger.info(f"Format: {format_type}, Remove watermark: {remove_watermark}")
        
        # Try to download the video
        video_path, video_id = get_tiktok_video(url, remove_watermark)
        
        if not video_path:
            return jsonify({"error": "Failed to download video after trying multiple methods"}), 500
        
        # If audio format is requested
        if format_type == 'audio':
            logger.info(f"Converting video to audio with quality: {audio_quality}")
            audio_path = convert_to_audio(video_path, video_id, audio_quality)
            
            if not audio_path:
                return jsonify({"error": "Failed to convert video to audio"}), 500
            
            # Set appropriate headers for download
            return send_file(
                audio_path, 
                as_attachment=True, 
                download_name=f"tiktok_{video_id}.mp3",
                mimetype="audio/mpeg"
            )
        else:
            # Set appropriate headers for download
            return send_file(
                video_path, 
                as_attachment=True, 
                download_name=f"tiktok_{video_id}.mp4",
                mimetype="video/mp4"
            )
        
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500

def _build_cors_preflight_response():
    """Build preflight response for CORS."""
    response = make_response()
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
    response.headers.add("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    return response

@app.after_request
def after_request(response):
    """Add headers to every response."""
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    response.headers.add('Cache-Control', 'public, max-age=3600')  # Cache for 1 hour
    return response

@app.route('/api/formats', methods=['GET'])
def get_formats():
    """Return available formats and qualities."""
    return jsonify({
        "formats": ["video", "audio"],
        "audio_qualities": list(AUDIO_QUALITY.keys()),
        "default_audio_quality": DEFAULT_AUDIO_QUALITY
    })

@app.route('/api/cleanup', methods=['POST'])
def force_cleanup():
    """Manually trigger cache cleanup."""
    try:
        cleanup_cache()
        return jsonify({"status": "ok", "message": "Cache cleanup completed"})
    except Exception as e:
        return jsonify({"error": f"Cache cleanup failed: {str(e)}"}), 500

if __name__ == '__main__':
    print("TikTok Downloader API Server")
    print("----------------------------")
    print("API Endpoints:")
    print("  - POST /api/download: Download TikTok videos/audio")
    print("  - GET /api/health: Health check endpoint")
    print("  - GET /api/formats: Get available formats and qualities")
    print("  - POST /api/cleanup: Force cache cleanup")
    print(f"  - Cache directory: {CACHE_DIR}")
    print(f"  - ffmpeg available: {'Yes' if ffmpeg_path else 'No'}")
    
    # Set host and port for Render or local development
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', 5000))
    
    print(f"\nServer is starting on http://{host}:{port}\n")
    
    # Run the Flask app
    app.run(
