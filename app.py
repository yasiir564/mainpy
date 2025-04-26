from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests
import re
import os
import tempfile
import logging
import time
import random
import json
from urllib.parse import urlparse
import subprocess
import hashlib
from functools import lru_cache
import shutil

# Check if ffmpeg is installed
try:
    subprocess.run(['ffmpeg', '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
except FileNotFoundError:
    print("Error: ffmpeg is not installed or not in PATH. Please install ffmpeg.")
    exit(1)

app = Flask(__name__)
# Configure CORS to allow specific origins in production or any in development
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configure temp directory for file storage
TEMP_DIR = os.path.join(tempfile.gettempdir(), 'tiktok_downloader')
os.makedirs(TEMP_DIR, exist_ok=True)

# Set cache size and expiration time (in seconds)
CACHE_SIZE = 100
CACHE_EXPIRATION = 3600  # 1 hour

# List of user agents to rotate
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_8_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; SM-A515F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.104 Mobile Safari/537.36",
    "Mozilla/5.0 (iPad; CPU OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
]

def get_random_user_agent():
    return random.choice(USER_AGENTS)

def generate_cache_key(url, format_type="mp3"):
    """Generate a unique cache key based on URL and format type."""
    key = f"{url}_{format_type}"
    return hashlib.md5(key.encode()).hexdigest()

def is_valid_tiktok_url(url):
    """Check if the URL is a valid TikTok URL."""
    parsed_url = urlparse(url)
    return parsed_url.netloc in ["www.tiktok.com", "tiktok.com", "vm.tiktok.com", "vt.tiktok.com"]

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
        r'tiktok\.com\/@[\w.-]+/video/(\d+)'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None

@lru_cache(maxsize=CACHE_SIZE)
def download_tiktok_video_mobile(video_id):
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
            r'"playAddr":"([^"]+)"',
            r'"downloadAddr":"([^"]+)"',
            r'"playUrl":"([^"]+)"',
            r'"videoUrl":"([^"]+)"',
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
        video_headers = {
            "User-Agent": get_random_user_agent(),
            "Referer": mobile_url,
            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.5",
            "Range": "bytes=0-",
            "DNT": "1",
            "Connection": "keep-alive"
        }
        
        video_response = requests.get(video_url, headers=video_headers, stream=True, timeout=30)
        
        if video_response.status_code not in [200, 206]:
            logger.error(f"Failed to download video. Status: {video_response.status_code}")
            return None
        
        # Create a temporary file
        temp_file = os.path.join(TEMP_DIR, f"{video_id}.mp4")
        
        # Stream the video to the file
        total_size = 0
        with open(temp_file, 'wb') as f:
            for chunk in video_response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    total_size += len(chunk)
        
        logger.info(f"Downloaded video file size: {total_size} bytes")
        
        if total_size < 10000:  # If file is too small, likely an error
            logger.error(f"Downloaded file is too small: {total_size} bytes")
            os.remove(temp_file)
            return None
            
        return temp_file
        
    except Exception as e:
        logger.error(f"Error downloading video: {e}")
        return None

@lru_cache(maxsize=CACHE_SIZE)
def download_tiktok_video_web(video_id):
    """Alternative method using web API."""
    try:
        # Build the web URL
        web_url = f"https://www.tiktok.com/api/item/detail/?itemId={video_id}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Referer": f"https://www.tiktok.com/video/{video_id}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": "tt_csrf_token=xxxx; tt_webid=xxxx"
        }
        
        logger.info(f"Fetching TikTok web API: {web_url}")
        response = requests.get(web_url, headers=headers, timeout=20)
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch TikTok web API. Status: {response.status_code}")
            return None
        
        try:
            data = response.json()
            
            # Navigate the JSON structure to find the video URL
            if "itemInfo" in data and "itemStruct" in data["itemInfo"]:
                video_data = data["itemInfo"]["itemStruct"]["video"]
                
                if "playAddr" in video_data:
                    video_url = video_data["playAddr"]
                elif "downloadAddr" in video_data:
                    video_url = video_data["downloadAddr"]
                else:
                    logger.error("Could not find video URL in API response")
                    return None
                
                # Download the video
                video_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                    "Referer": f"https://www.tiktok.com/video/{video_id}",
                    "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
                    "Accept-Language": "en-US,en;q=0.9"
                }
                
                video_response = requests.get(video_url, headers=video_headers, stream=True, timeout=30)
                
                if video_response.status_code != 200:
                    logger.error(f"Failed to download video from API. Status: {video_response.status_code}")
                    return None
                
                # Create a temporary file
                temp_file = os.path.join(TEMP_DIR, f"{video_id}.mp4")
                
                # Stream the video to the file
                with open(temp_file, 'wb') as f:
                    for chunk in video_response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                
                if os.path.getsize(temp_file) < 10000:
                    logger.error(f"Downloaded file is too small: {os.path.getsize(temp_file)} bytes")
                    os.remove(temp_file)
                    return None
                    
                return temp_file
            else:
                logger.error("Unexpected API response structure")
                return None
                
        except json.JSONDecodeError:
            logger.error("Failed to parse API response as JSON")
            return None
            
    except Exception as e:
        logger.error(f"Error in web API method: {e}")
        return None

@lru_cache(maxsize=CACHE_SIZE)
def download_tiktok_video_embed(video_id):
    """Try downloading via TikTok's embed functionality."""
    try:
        # Build the embed URL
        embed_url = f"https://www.tiktok.com/embed/v2/{video_id}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
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
        video_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Referer": embed_url,
            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.5"
        }
        
        video_response = requests.get(video_url, headers=video_headers, stream=True, timeout=30)
        
        if video_response.status_code != 200:
            logger.error(f"Failed to download video from embed. Status: {video_response.status_code}")
            return None
        
        # Create a temporary file
        temp_file = os.path.join(TEMP_DIR, f"{video_id}.mp4")
        
        # Stream the video to the file
        with open(temp_file, 'wb') as f:
            for chunk in video_response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        if os.path.getsize(temp_file) < 10000:
            logger.error(f"Downloaded file is too small: {os.path.getsize(temp_file)} bytes")
            os.remove(temp_file)
            return None
            
        return temp_file
        
    except Exception as e:
        logger.error(f"Error in embed method: {e}")
        return None

def convert_video_to_mp3(video_path, video_id):
    """Convert video to MP3 using ffmpeg."""
    try:
        mp3_path = os.path.join(TEMP_DIR, f"{video_id}.mp3")
        
        # -y: Overwrite output file without asking
        # -i: Input file
        # -q:a 2: Audio quality (0-9, 0 = best, 9 = worst)
        # -map a: Only extract audio stream
        # -vn: No video
        cmd = [
            'ffmpeg',
            '-y',
            '-i', video_path,
            '-q:a', '2',
            '-map', 'a',
            '-vn',
            mp3_path
        ]
        
        logger.info(f"Converting video to MP3: {' '.join(cmd)}")
        process = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        
        if process.returncode != 0:
            logger.error(f"FFmpeg error: {process.stderr.decode()}")
            return None
        
        logger.info(f"Successfully converted video to MP3. File size: {os.path.getsize(mp3_path)} bytes")
        return mp3_path
    except Exception as e:
        logger.error(f"Error converting video to MP3: {e}")
        return None

def get_tiktok_video(url):
    """Try multiple methods to download TikTok video."""
    # Extract video ID from URL
    video_id = extract_video_id(url)
    if not video_id:
        logger.error(f"Could not extract video ID from URL: {url}")
        return None, None
    
    logger.info(f"Extracted video ID: {video_id}")
    
    # Check if we already have the MP3 cached
    mp3_path = os.path.join(TEMP_DIR, f"{video_id}.mp3")
    if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
        logger.info(f"Using cached MP3 file: {mp3_path}")
        return mp3_path, video_id
    
    # List of methods to try in order
    methods = [
        download_tiktok_video_mobile,
        download_tiktok_video_web,
        download_tiktok_video_embed
    ]
    
    for method in methods:
        try:
            logger.info(f"Trying download method: {method.__name__}")
            video_path = method(video_id)
            
            if video_path:
                logger.info(f"Successfully downloaded video using {method.__name__}")
                
                # Convert video to MP3
                mp3_path = convert_video_to_mp3(video_path, video_id)
                if mp3_path:
                    return mp3_path, video_id
                    
            # Wait a moment before trying the next method to avoid rate limiting
            time.sleep(1)
        except Exception as e:
            logger.error(f"Error in download method {method.__name__}: {e}")
    
    logger.error("All download methods failed")
    return None, video_id

def cleanup_old_files():
    """Clean up old temporary files to prevent disk space issues."""
    try:
        current_time = time.time()
        for filename in os.listdir(TEMP_DIR):
            file_path = os.path.join(TEMP_DIR, filename)
            # Remove files older than cache expiration time
            if os.path.isfile(file_path) and current_time - os.path.getmtime(file_path) > CACHE_EXPIRATION:
                os.remove(file_path)
                logger.info(f"Removed old file: {filename}")
    except Exception as e:
        logger.error(f"Error cleaning up old files: {e}")

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint to verify service is running."""
    return jsonify({"status": "ok", "message": "TikTok to MP3 converter service is running"})

@app.route('/api/convert', methods=['POST'])
def convert_tiktok_to_mp3():
    """API endpoint to download TikTok videos and convert to MP3."""
    try:
        data = request.json
        if not data:
            return jsonify({"error": "Invalid JSON data"}), 400
            
        url = data.get('url')
        
        if not url:
            return jsonify({"error": "No URL provided"}), 400
        
        if not is_valid_tiktok_url(url):
            return jsonify({"error": "Invalid TikTok URL"}), 400
        
        # Clean up old files occasionally to prevent disk space issues
        if random.random() < 0.1:  # 10% chance to trigger cleanup
            cleanup_old_files()
        
        # Try to download the video and convert to MP3
        mp3_path, video_id = get_tiktok_video(url)
        
        if not mp3_path:
            return jsonify({"error": "Failed to download and convert video"}), 500
            
        # Set appropriate headers for download
        return send_file(
            mp3_path, 
            as_attachment=True, 
            download_name=f"tiktok_{video_id}.mp3",
            mimetype="audio/mpeg"
        )
        
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """API endpoint to get service statistics."""
    try:
        cache_files = len([f for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f))])
        mp3_files = len([f for f in os.listdir(TEMP_DIR) if f.endswith('.mp3')])
        mp4_files = len([f for f in os.listdir(TEMP_DIR) if f.endswith('.mp4')])
        
        return jsonify({
            "status": "ok",
            "stats": {
                "cache_files": cache_files,
                "mp3_files": mp3_files,
                "mp4_files": mp4_files,
                "cache_dir_size_mb": sum(os.path.getsize(os.path.join(TEMP_DIR, f)) for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f))) / (1024 * 1024)
            }
        })
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return jsonify({"error": f"Error getting stats: {str(e)}"}), 500

if __name__ == '__main__':
    # Create a README file for Render deployment
    readme_path = 'README.md'
    with open(readme_path, 'w') as f:
        f.write("""# TikTok to MP3 Converter API

A Flask-based API service that downloads TikTok videos and converts them to MP3 format.

## Requirements
- Python 3.8+
- FFmpeg must be installed on the system

## Environment Variables
None required

## API Endpoints
- POST /api/convert: Convert TikTok video to MP3
- GET /api/health: Health check endpoint
- GET /api/stats: Get service statistics

## Deployment
This service is designed to be deployed on Render.com.

### Render.com Setup
1. Create a new Web Service
2. Use the Docker Runtime
3. No environment variables needed
4. Make sure to install FFmpeg in your build script
""")

    # Create a Dockerfile for Render deployment
    dockerfile_path = 'Dockerfile'
    with open(dockerfile_path, 'w') as f:
        f.write("""FROM python:3.9-slim

WORKDIR /app

# Install FFmpeg and other dependencies
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 8080

# Command to run the application
CMD gunicorn --bind 0.0.0.0:8080 app:app
""")

    # Create requirements.txt for dependencies
    requirements_path = 'requirements.txt'
    with open(requirements_path, 'w') as f:
        f.write("""flask==2.0.1
flask-cors==3.0.10
requests==2.26.0
gunicorn==20.1.0
""")

    print("TikTok to MP3 Converter API Server")
    print("----------------------------")
    print("API Endpoints:")
    print("  - POST /api/convert: Convert TikTok videos to MP3")
    print("  - GET /api/health: Health check endpoint")
    print("  - GET /api/stats: Get service statistics")
    print("\nServer is starting on http://0.0.0.0:8080\n")
    
    # Run the Flask app with gunicorn configuration for production
    port = int(os.environ.get("PORT", 8080))
    app.run(
        host='0.0.0.0',  # Allow external connections for production
        port=port,
        debug=False,
        threaded=True
    )
