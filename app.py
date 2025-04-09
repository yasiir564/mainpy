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
from flask_cors import CORS  # Added for better CORS handling
from functools import wraps, lru_cache
import threading
import urllib.parse

app = Flask(__name__)
# Configure CORS properly to allow your domain
CORS(app, resources={
    "/api/*": {
        "origins": ["https://tokhaste.com", "http://localhost:3000"],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    },
    "/download/*": {
        "origins": ["https://tokhaste.com", "http://localhost:3000"],
        "methods": ["GET"]
    },
    "/status": {
        "origins": ["https://tokhaste.com", "http://localhost:3000"],
        "methods": ["GET"]
    }
})

# Configuration
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(CURRENT_DIR, "downloads/")
OUTPUT_DIR = os.path.join(CURRENT_DIR, "mp3/")
CACHE_EXPIRY = 86400  # 24 hours (in seconds)
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB max file size
MAX_RETRIES = 3  # Maximum number of retries for API requests
MAX_DOWNLOAD_RETRIES = 5  # Maximum retries for downloading
DOWNLOAD_TIMEOUT = 60  # Timeout for download requests in seconds
CONVERSION_TIMEOUT = 180  # Timeout for conversion process in seconds

# Create directories if they don't exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('tiktok_to_mp3')

# Add a file handler for persistent logging
file_handler = logging.FileHandler(os.path.join(CURRENT_DIR, 'app.log'))
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)

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
    with cache_lock:
        if url_hash in video_cache and video_cache[url_hash]['expires'] > time.time():
            log_message(f'Cache hit for TikTok: {url_hash}')
            return video_cache[url_hash]['data']
    return False

def set_tiktok_cache(url_hash, data, expiration=CACHE_EXPIRY):
    with cache_lock:
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

# Try to get TikTok video using SnaptikAPI
def fetch_from_snaptik(url):
    log_message(f'Trying Snaptik API service for: {url}')
    
    api_url = 'https://snaptik.app/abc2.php'
    encoded_url = urllib.parse.quote(url)
    
    try:
        session = requests.Session()
        # First get the token
        response = session.get(
            'https://snaptik.app/en',
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            },
            timeout=30
        )
        
        if response.status_code != 200:
            log_message('Failed to access Snaptik service')
            return None
        
        # Extract token
        token_match = re.search(r'var\s+token\s*=\s*"([^"]+)"', response.text)
        if not token_match:
            log_message('Failed to extract token from Snaptik')
            return None
        
        token = token_match.group(1)
        
        # Make API request
        response = session.post(
            api_url,
            data={
                'url': encoded_url,
                'token': token
            },
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'Origin': 'https://snaptik.app',
                'Referer': 'https://snaptik.app/en',
                'X-Requested-With': 'XMLHttpRequest'
            },
            timeout=30
        )
        
        if response.status_code != 200:
            log_message(f'Failed to get a response from Snaptik API: {response.status_code}')
            return None
        
        response_text = response.text
        
        # Parse response HTML to find download links
        video_match = re.search(r'href="(https://[^"]+\.mp4[^"]*)"', response_text)
        if not video_match:
            log_message('Failed to extract download link from Snaptik response')
            return None
        
        video_url = video_match.group(1).replace('&amp;', '&')
        
        # Try to extract author
        author = 'Unknown'
        author_match = re.search(r'<h2[^>]*>@([^<]+)</h2>', response_text)
        if author_match:
            author = author_match.group(1)
        
        # Try to extract description
        desc = ''
        desc_match = re.search(r'<p[^>]*class="[^"]*text[^"]*"[^>]*>([^<]+)</p>', response_text)
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
            'method': 'snaptik'
        }
    except Exception as e:
        log_message(f'Error using Snaptik service: {str(e)}')
        return None

# Direct approach for TikTok URLs
def fetch_direct_tiktok(url):
    log_message(f'Trying direct extraction for: {url}')
    
    try:
        # Make sure we're working with the full URL after any redirects
        if 'vm.tiktok.com' in url or 'vt.tiktok.com' in url:
            url = follow_tiktok_redirects(url)
        
        # Request the TikTok page with mobile user agent
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://www.tiktok.com/',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-User': '?1',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache',
        }
        
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code != 200:
            log_message(f'Failed to access TikTok page: {response.status_code}')
            return None
        
        html_content = response.text
        
        # Look for video URLs in the HTML content
        # Try to find mp4 URL
        video_url_match = re.search(r'(https://v[^"\']+\.mp4[^"\']*)["\']\s*,', html_content)
        if not video_url_match:
            video_url_match = re.search(r'"(https://v[^"\']+\.mp4[^"\']*)"', html_content)
        
        if not video_url_match:
            log_message('Could not find video URL in TikTok page')
            return None
            
        video_url = video_url_match.group(1).replace('\\u002F', '/').replace('\\', '')
        
        # Try to extract the author username
        author = 'TikToker'
        author_match = re.search(r'"uniqueId"\s*:\s*"([^"]+)"', html_content)
        if author_match:
            author = author_match.group(1)
        
        # Try to extract the video description
        desc = ''
        desc_match = re.search(r'"description"\s*:\s*"([^"]+)"', html_content)
        if desc_match:
            desc = desc_match.group(1).replace('\\n', ' ').replace('\\', '')
        
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
            'method': 'direct'
        }
    except Exception as e:
        log_message(f'Error with direct TikTok extraction: {str(e)}')
        return None

# New method: Try to get TikTok video using TiTok-DL API
def fetch_from_tikdownloader(url):
    log_message(f'Trying TikDownloader service for: {url}')
    
    api_url = 'https://tikdown.org/getAjax'
    
    try:
        session = requests.Session()
        
        # First request to get cookies
        response = session.get(
            'https://tikdown.org/',
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            },
            timeout=30
        )
        
        if response.status_code != 200:
            log_message('Failed to access TikDownloader service')
            return None
        
        # Make the API request
        response = session.post(
            api_url,
            data={
                'url': url
            },
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': 'https://tikdown.org',
                'Referer': 'https://tikdown.org/'
            },
            timeout=30
        )
        
        if response.status_code != 200:
            log_message(f'Failed to get a response from TikDownloader API: {response.status_code}')
            return None
        
        data = response.json()
        
        if not data.get('success'):
            log_message(f'TikDownloader API returned error: {data}')
            return None
        
        # Extract the download link from HTML response
        html_content = data.get('html', '')
        
        video_match = re.search(r'href="([^"]+)"\s*[^>]*>\s*Download\s*MP4', html_content)
        if not video_match:
            log_message('Failed to extract download link from TikDownloader response')
            return None
        
        video_url = video_match.group(1)
        
        # Try to extract author and description from HTML
        author = 'Unknown'
        author_match = re.search(r'<h2[^>]*>\s*([^<]+)\s*</h2>', html_content)
        if author_match:
            author = author_match.group(1).strip()
        
        desc = ''
        desc_match = re.search(r'<div[^>]*class="[^"]*desc[^"]*"[^>]*>(.*?)</div>', html_content, re.DOTALL)
        if desc_match:
            # Remove HTML tags
            desc = re.sub(r'<[^>]+>', ' ', desc_match.group(1))
            desc = re.sub(r'\s+', ' ', desc).strip()
        
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
            'method': 'tikdownloader'
        }
    except Exception as e:
        log_message(f'Error using TikDownloader service: {str(e)}')
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
    
    # Try multiple methods to get video info
    result = None
    methods = [
        fetch_from_tikwm,
        fetch_from_snaptik,
        fetch_from_tikdownloader,  # Added new method
        fetch_from_ssstik,
        fetch_direct_tiktok  # Direct extraction as last resort
    ]
    
    for method in methods:
        result = method(url)
        if result and result.get('video_url'):
            log_message(f"Successfully extracted video using {result['method']} method")
            break
    
    if not result or not result.get('video_url'):
        raise Exception("Failed to extract video URL from TikTok link - tried all available methods")
    
    video_url = result['video_url']
    author = result['author']
    
    # Generate a nice filename based on the author and a unique ID
    filename = f"{sanitize_filename(author)}_{uuid.uuid4().hex[:8]}.mp4"
    file_path = os.path.join(UPLOAD_DIR, filename)
    
    # Download the video with retry mechanism
    log_message(f"Downloading video from {video_url} to {file_path}")
    
    for attempt in range(MAX_DOWNLOAD_RETRIES):
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Referer': 'https://www.tiktok.com/',
                'Accept': '*/*',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive'
            }
            
            response = requests.get(video_url, stream=True, timeout=DOWNLOAD_TIMEOUT, headers=headers)
            
            if response.status_code != 200:
                log_message(f"Attempt {attempt+1} failed with status {response.status_code}")
                if attempt == MAX_DOWNLOAD_RETRIES - 1:
                    raise Exception(f"Failed to download video after {MAX_DOWNLOAD_RETRIES} attempts: HTTP {response.status_code}")
                time.sleep(1)  # Wait before retrying
                continue
            
            # Check if we're getting HTML instead of video data
            content_type = response.headers.get('Content-Type', '')
            if 'text/html' in content_type and len(response.content) < 1000000:  # Less than 1MB is probably HTML
                log_message(f"Received HTML instead of video data on attempt {attempt+1}")
                if attempt == MAX_DOWNLOAD_RETRIES - 1:
                    raise Exception("Received HTML instead of video data")
                time.sleep(1)  # Wait before retrying
                continue
                
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            # Verify file exists and has a reasonable size
            if not os.path.exists(file_path) or os.path.getsize(file_path) < 1024:
                log_message(f"Downloaded file is too small or doesn't exist on attempt {attempt+1}")
                if attempt == MAX_DOWNLOAD_RETRIES - 1:
                    raise Exception("Downloaded file is too small or doesn't exist")
                time.sleep(1)  # Wait before retrying
                continue
                
            # Validate that the file is a valid video file
            try:
                validate_cmd = ["ffprobe", "-v", "error", file_path]
                validate_process = subprocess.run(
                    validate_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10
                )
                
                if validate_process.returncode != 0:
                    log_message(f"Downloaded file is not a valid video on attempt {attempt+1}")
                    if attempt == MAX_DOWNLOAD_RETRIES - 1:
                        raise Exception("Downloaded file is not a valid video")
                    os.remove(file_path)
                    time.sleep(1)  # Wait before retrying
                    continue
            except subprocess.TimeoutExpired:
                log_message(f"Validation timeout on attempt {attempt+1}")
                if attempt == MAX_DOWNLOAD_RETRIES - 1:
                    raise Exception("Video validation timed out")
                os.remove(file_path)
                time.sleep(1)
                continue
                
            break  # Successful download
            
        except Exception as e:
            log_message(f"Download attempt {attempt+1} failed: {str(e)}")
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
                
            if attempt == MAX_DOWNLOAD_RETRIES - 1:
                raise Exception(f"Failed to download video after {MAX_DOWNLOAD_RETRIES} attempts: {str(e)}")
            time.sleep(1)  # Wait before retrying
    
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
    
    log_message(f"Converting {video_path} to MP3 at {output_path}")
    
    # Prepare FFmpeg command for conversion
    cmd = [
        "ffmpeg",
        "-i", video_path,
        "-vn",  # Skip video
        "-acodec", "libmp3lame",
        "-ab", "192k",
        "-ar", "44100",
        "-y",  # Overwrite output file
        output_path
    ]
    
    try:
        # Execute the conversion
        conversion_process = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CONVERSION_TIMEOUT
        )
        
        if conversion_process.returncode != 0:
            error_message = conversion_process.stderr.decode('utf-8', errors='ignore')
            log_message(f"FFmpeg conversion failed: {error_message}")
            raise Exception(f"FFmpeg conversion failed: {error_message[:200]}...")
            
        # Add metadata to the MP3 file
        cmd_metadata = [
            "ffmpeg",
            "-i", output_path,
            "-c", "copy",
            "-metadata", f"title={desc[:30] or 'TikTok Audio'}",
            "-metadata", f"artist={author or 'TikTok'}",
            "-metadata", f"album=Downloaded via tokhaste.com",
            "-y",
            f"{output_path}.temp.mp3"
        ]
        
        metadata_process = subprocess.run(
            cmd_metadata,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30
        )
        
        if metadata_process.returncode == 0 and os.path.exists(f"{output_path}.temp.mp3"):
            os.replace(f"{output_path}.temp.mp3", output_path)
        else:
            log_message("Failed to add metadata, using original conversion")
            
        # Verify the MP3 file exists and has a reasonable size
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
            raise Exception("Converted MP3 file is too small or doesn't exist")
            
        return output_path
        
    except subprocess.TimeoutExpired:
        log_message("FFmpeg conversion timed out")
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except:
                pass
        raise Exception("FFmpeg conversion timed out")
        
    except Exception as e:
        log_message(f"Error during conversion: {str(e)}")
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except:
                pass
        raise Exception(f"Error during conversion: {str(e)}")

def process_tiktok_url(url):
    """Process a TikTok URL to download and convert to MP3"""
    url_hash = hashlib.md5(url.encode()).hexdigest()
    
    # Check if we have cached info for this URL
    cached_data = get_tiktok_cache(url_hash)
    if cached_data and os.path.exists(cached_data['mp3_path']):
        log_message(f"Using cached MP3 for {url}")
        return cached_data
    
    # Download the TikTok video
    video_info = download_tiktok_video(url)
    video_path = video_info['file_path']
    
    try:
        # Convert to MP3
        mp3_path = convert_to_mp3(video_path, video_info['author'], video_info['desc'])
        
        # Cache the result
        result = {
            'mp3_path': mp3_path,
            'author': video_info['author'],
            'desc': video_info['desc'],
            'filename': os.path.basename(mp3_path)
        }
        set_tiktok_cache(url_hash, result)
        
        # Cache the file info for cleanup
        with cache_lock:
            file_cache[mp3_path] = {
                "last_accessed": time.time(),
                "output_path": mp3_path
            }
        
        return result
        
    finally:
        # Clean up the video file
        try:
            if os.path.exists(video_path):
                os.remove(video_path)
        except Exception as e:
            log_message(f"Failed to clean up video file: {str(e)}")

def validate_tiktok_url(url):
    """Check if the URL appears to be a valid TikTok URL"""
    if not url:
        return False
        
    url = url.strip().lower()
    
    # Check if the URL starts with http/https
    if not (url.startswith('http://') or url.startswith('https://')):
        url = 'https://' + url
    
    # Various TikTok domain patterns
    tiktok_patterns = [
        r'tiktok\.com\/@[\w\.]+\/video\/\d+',
        r'tiktok\.com\/t\/\w+',
        r'vm\.tiktok\.com\/\w+',
        r'vt\.tiktok\.com\/\w+',
        r'm\.tiktok\.com\/',
        r'tiktok\.com\/.*\?.*item_id=\d+'
    ]
    
    for pattern in tiktok_patterns:
        if re.search(pattern, url):
            return url
    
    return False

# API Endpoints
@app.route('/status', methods=['GET'])
def status():
    """API health check endpoint"""
    try:
        # Get FFmpeg version
        ffmpeg_version = get_ffmpeg_version()
        
        # Get cache stats
        video_cache_count = len(video_cache)
        file_cache_count = len(file_cache)
        
        # Check directories
        upload_dir_exists = os.path.exists(UPLOAD_DIR)
        output_dir_exists = os.path.exists(OUTPUT_DIR)
        
        return jsonify({
            'status': 'ok',
            'ffmpeg_version': ffmpeg_version,
            'cache': {
                'video_cache_count': video_cache_count,
                'file_cache_count': file_cache_count
            },
            'directories': {
                'upload_dir_exists': upload_dir_exists,
                'output_dir_exists': output_dir_exists
            },
            'version': '1.2.0'
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500

@app.route('/api/extract', methods=['POST'])
def extract_audio():
    """API endpoint to extract audio from a TikTok URL"""
    try:
        # Get the TikTok URL from request
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({
                'status': 'error',
                'message': 'No URL provided'
            }), 400
            
        url = data['url']
        
        # Validate the URL
        valid_url = validate_tiktok_url(url)
        if not valid_url:
            return jsonify({
                'status': 'error',
                'message': 'Invalid TikTok URL'
            }), 400
            
        url = valid_url  # Use the validated URL
        
        # Process the URL to download and convert to MP3
        result = process_tiktok_url(url)
        
        if not result or not os.path.exists(result['mp3_path']):
            return jsonify({
                'status': 'error',
                'message': 'Failed to process TikTok URL'
            }), 500
            
        # Generate a download URL for the MP3 file
        filename = result['filename']
        download_url = f"/download/{filename}"
        
        return jsonify({
            'status': 'success',
            'download_url': download_url,
            'author': result['author'],
            'desc': result['desc'],
            'filename': filename
        })
        
    except Exception as e:
        log_message(f"Error processing request: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    """API endpoint to download a processed MP3 file"""
    try:
        # Sanitize the filename
        filename = sanitize_filename(filename)
        file_path = os.path.join(OUTPUT_DIR, filename)
        
        if not os.path.exists(file_path):
            return jsonify({
                'status': 'error',
                'message': 'File not found'
            }), 404
            
        # Update last accessed time for cache management
        with cache_lock:
            if file_path in file_cache:
                file_cache[file_path]['last_accessed'] = time.time()
            else:
                # Add to cache if not present
                file_cache[file_path] = {
                    'last_accessed': time.time(),
                    'output_path': file_path
                }
        
        # Create a download response
        response = make_response(send_file(file_path, mimetype='audio/mpeg'))
        response.headers['Content-Disposition'] = f'attachment; filename={filename}'
        return response
        
    except Exception as e:
        log_message(f"Error serving file: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

# Start the cleanup thread
start_cleanup_thread()

if __name__ == '__main__':
    # Run the Flask app
    app.run(host='0.0.0.0', port=5000)
