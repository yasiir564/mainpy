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
import threading
from datetime import datetime

# Check if ffmpeg is installed
try:
    subprocess.run(['ffmpeg', '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
except (FileNotFoundError, subprocess.CalledProcessError):
    print("Error: ffmpeg is not installed or not in PATH. Please install ffmpeg.")
    exit(1)

app = Flask(__name__)
# Configure CORS to allow specific origins in production or any in development
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Cloudflare Turnstile Configuration
TURNSTILE_SECRET_KEY = "0x4AAAAAABHoxYr9SKSH_1ZBB4LpXbr_0sQ"  # Replace with your actual secret key
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("tiktok_converter.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configure temp directory for file storage
TEMP_DIR = os.path.join(tempfile.gettempdir(), 'tiktok_downloader')
os.makedirs(TEMP_DIR, exist_ok=True)

# Set cache size and expiration time (in seconds)
CACHE_SIZE = 200
CACHE_EXPIRATION = 86400  # 24 hours
MAX_CACHE_SIZE_MB = 5000  # 5GB maximum cache size

# List of user agents to rotate - expanded for better undetectability
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/119.0.6045.109 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.5993.80 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 312.0.0.0.41",
    "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Mobile Safari/537.36"
]

# List of cookies to rotate
TT_COOKIES = [
    "tt_webid_v2=123456789012345678; tt_webid=123456789012345678; ttwid=1%7CAbC123dEf456gHi789jKl%7C1600000000%7Cabcdef0123456789abcdef0123456789; msToken=AbC123dEf456gHi789jKl",
    "tt_webid_v2=234567890123456789; tt_webid=234567890123456789; ttwid=1%7CBcD234eFg567hIj890kLm%7C1600100000%7Cbcdefg1234567890abcdef0123456789; msToken=BcD234eFg567hIj890kLm",
    "tt_webid_v2=345678901234567890; tt_webid=345678901234567890; ttwid=1%7CCdE345fGh678iJk901lMn%7C1600200000%7Ccdefgh2345678901abcdef0123456789; msToken=CdE345fGh678iJk901lMn",
]

# Proxy configuration (optional)
# Format: {"http": "http://user:pass@host:port", "https": "http://user:pass@host:port"}
PROXIES = None

# Download rate limiting
MAX_DOWNLOADS_PER_MINUTE = 20
download_timestamps = []
download_lock = threading.Lock()

# Track active downloads
active_downloads = {}
active_downloads_lock = threading.Lock()

class DownloadStats:
    def __init__(self):
        self.total_downloads = 0
        self.successful_downloads = 0
        self.failed_downloads = 0
        self.last_download_time = None
        self.lock = threading.Lock()
        
    def increment_total(self):
        with self.lock:
            self.total_downloads += 1
            self.last_download_time = datetime.now()
            
    def increment_success(self):
        with self.lock:
            self.successful_downloads += 1
            
    def increment_failed(self):
        with self.lock:
            self.failed_downloads += 1
            
    def get_stats(self):
        with self.lock:
            return {
                "total": self.total_downloads,
                "successful": self.successful_downloads,
                "failed": self.failed_downloads,
                "last_download": self.last_download_time.isoformat() if self.last_download_time else None
            }

stats = DownloadStats()

def get_random_user_agent():
    """Get a random user agent from the list."""
    return random.choice(USER_AGENTS)

def get_random_cookies():
    """Get random cookies for TikTok requests."""
    return random.choice(TT_COOKIES)

def can_perform_download():
    """Rate limiting for downloads."""
    global download_timestamps
    
    with download_lock:
        current_time = time.time()
        # Remove timestamps older than 60 seconds
        download_timestamps = [ts for ts in download_timestamps if current_time - ts < 60]
        
        if len(download_timestamps) >= MAX_DOWNLOADS_PER_MINUTE:
            return False
        
        download_timestamps.append(current_time)
        return True

def verify_turnstile_token(token, remote_ip=None):
    """Verify Cloudflare Turnstile token."""
    try:
        data = {
            "secret": TURNSTILE_SECRET_KEY,
            "response": token
        }
        
        if remote_ip:
            data["remoteip"] = remote_ip
            
        response = requests.post(TURNSTILE_VERIFY_URL, data=data, timeout=10)
        result = response.json()
        
        if result.get("success"):
            return True, None
        else:
            return False, result.get("error-codes", ["Unknown error"])
    except Exception as e:
        logger.error(f"Turnstile verification error: {e}")
        return False, ["Verification service error"]

def generate_cache_key(url, format_type="mp3", quality="192"):
    """Generate a unique cache key based on URL, format type and quality."""
    key = f"{url}_{format_type}_{quality}"
    return hashlib.md5(key.encode()).hexdigest()

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
    if any(domain in url for domain in ["vm.tiktok.com", "vt.tiktok.com"]):
        url = expand_shortened_url(url)
    
    # Extract video ID from URL
    patterns = [
        r'/video/(\d+)',
        r'tiktok\.com\/@[\w.-]+/video/(\d+)',
        r'v/(\d+)'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None

def get_random_request_headers(referer=None):
    """Generate randomized headers for HTTP requests."""
    headers = {
        "User-Agent": get_random_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache"
    }
    
    if referer:
        headers["Referer"] = referer
        
    # Add cookies to some requests for better undetectability
    if random.random() < 0.7:  # 70% chance to add cookies
        headers["Cookie"] = get_random_cookies()
    
    return headers

@lru_cache(maxsize=CACHE_SIZE)
def download_tiktok_video_mobile(video_id):
    """Download TikTok video using the mobile website."""
    try:
        # Direct video URL
        mobile_url = f"https://m.tiktok.com/v/{video_id}"
        
        headers = get_random_request_headers()
        
        logger.info(f"Fetching mobile TikTok page: {mobile_url}")
        response = requests.get(
            mobile_url, 
            headers=headers, 
            timeout=20,
            proxies=PROXIES
        )
        
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
        video_headers = get_random_request_headers(referer=mobile_url)
        video_headers.update({
            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
            "Range": "bytes=0-"
        })
        
        video_response = requests.get(
            video_url, 
            headers=video_headers, 
            stream=True, 
            timeout=30,
            proxies=PROXIES
        )
        
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
        
        headers = get_random_request_headers(referer=f"https://www.tiktok.com/video/{video_id}")
        headers.update({
            "Accept": "application/json, text/plain, */*"
        })
        
        logger.info(f"Fetching TikTok web API: {web_url}")
        response = requests.get(
            web_url, 
            headers=headers, 
            timeout=20,
            proxies=PROXIES
        )
        
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
                video_headers = get_random_request_headers(referer=f"https://www.tiktok.com/video/{video_id}")
                video_headers.update({
                    "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5"
                })
                
                video_response = requests.get(
                    video_url, 
                    headers=video_headers, 
                    stream=True, 
                    timeout=30,
                    proxies=PROXIES
                )
                
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
        
        headers = get_random_request_headers()
        
        logger.info(f"Fetching TikTok embed page: {embed_url}")
        response = requests.get(
            embed_url, 
            headers=headers, 
            timeout=20,
            proxies=PROXIES
        )
        
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
        video_headers = get_random_request_headers(referer=embed_url)
        video_headers.update({
            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5"
        })
        
        video_response = requests.get(
            video_url, 
            headers=video_headers, 
            stream=True, 
            timeout=30,
            proxies=PROXIES
        )
        
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

@lru_cache(maxsize=CACHE_SIZE)
def download_tiktok_video_scraper(video_id):
    """Try downloading using a more sophisticated approach."""
    try:
        # Build the direct video URL
        url = f"https://www.tiktok.com/@tiktok/video/{video_id}"
        headers = get_random_request_headers()
        
        # Add specific headers that may help bypass restrictions
        headers.update({
            "sec-ch-ua": '"Chromium";v="118", "Google Chrome";v="118"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1"
        })
        
        logger.info(f"Fetching TikTok page with scraper method: {url}")
        response = requests.get(
            url, 
            headers=headers, 
            timeout=30,
            proxies=PROXIES
        )
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch TikTok page. Status: {response.status_code}")
            return None
            
        # Try to find the video data in the page
        # Look for the __UNIVERSAL_DATA_FOR_REHYDRATION__ script
        universal_data_match = re.search(r'window\["UNIVERSAL_DATA_FOR_REHYDRATION"\]\s*=\s*({.+?});', response.text)
        if universal_data_match:
            try:
                universal_data_str = universal_data_match.group(1)
                universal_data = json.loads(universal_data_str)
                
                # Navigate through the structure to find video URL
                if "state" in universal_data and "ItemModule" in universal_data["state"]:
                    item_module = universal_data["state"]["ItemModule"]
                    if video_id in item_module:
                        video_data = item_module[video_id]["video"]
                        video_url = video_data.get("playAddr") or video_data.get("downloadAddr")
                        
                        if video_url:
                            logger.info(f"Found video URL via universal data: {video_url[:60]}...")
                            
                            # Download the video
                            video_headers = get_random_request_headers(referer=url)
                            video_response = requests.get(
                                video_url, 
                                headers=video_headers, 
                                stream=True, 
                                timeout=30,
                                proxies=PROXIES
                            )
                            
                            if video_response.status_code != 200:
                                logger.error(f"Failed to download video. Status: {video_response.status_code}")
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
            except json.JSONDecodeError:
                logger.error("Failed to parse universal data JSON")
        
        # If we reached here, try regex method as fallback
        patterns = [
            r'"playAddr":"([^"]+)"',
            r'"downloadAddr":"([^"]+)"',
            r'"playUrl":"([^"]+)"',
            r'"contentUrl":"([^"]+)"',
            r'<video[^>]+src="([^"]+)"'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, response.text)
            if matches:
                video_url = matches[0]
                video_url = video_url.replace('\\u002F', '/').replace('\\', '')
                logger.info(f"Found video URL via regex: {video_url[:60]}...")
                
                # Download the video
                video_headers = get_random_request_headers(referer=url)
                video_response = requests.get(
                    video_url, 
                    headers=video_headers, 
                    stream=True, 
                    timeout=30,
                    proxies=PROXIES
                )
                
                if video_response.status_code != 200:
                    logger.error(f"Failed to download video. Status: {video_response.status_code}")
                    continue
                
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
                    continue
                    
                return temp_file
        
        return None
    except Exception as e:
        logger.error(f"Error in scraper method: {e}")
        return None

def convert_video_to_mp3(video_path, video_id, quality="192"):
    """Convert video to MP3 using ffmpeg with specified quality."""
    try:
        # Map quality string to bitrate
        quality_bitrates = {
            "128": "128k",
            "192": "192k",
            "256": "256k",
            "320": "320k"
        }
        
        bitrate = quality_bitrates.get(quality, "192k")
        mp3_path = os.path.join(TEMP_DIR, f"{video_id}_{quality}.mp3")
        
        # FFmpeg command with improved audio quality options
        cmd = [
            'ffmpeg',
            '-y',  # Overwrite output file without asking
            '-i', video_path,  # Input file
            '-vn',  # No video
            '-ar', '44100',  # Audio sample rate: 44.1kHz
            '-ac', '2',  # Audio channels: stereo
            '-b:a', bitrate,  # Audio bitrate
            '-f', 'mp3',  # Force format
            mp3_path
        ]
        
        logger.info(f"Converting video to MP3 at {bitrate} quality")
        process = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        
        if process.returncode != 0:
            logger.error(f"FFmpeg error: {process.stderr.decode()}")
            return None
        
        logger.info(f"Successfully converted video to MP3. File size: {os.path.getsize(mp3_path)} bytes")
        return mp3_path
    except Exception as e:
        logger.error(f"Error converting video to MP3: {e}")
        return None

def get_tiktok_video(url, quality="192"):
    """Try multiple methods to download TikTok video."""
    # Extract video ID from URL
    video_id = extract_video_id(url)
    if not video_id:
        logger.error(f"Could not extract video ID from URL: {url}")
        return None, None
    
    logger.info(f"Extracted video ID: {video_id}")
    
    # Check if we already have the MP3 cached at the requested quality
    mp3_path = os.path.join(TEMP_DIR, f"{video_id}_{quality}.mp3")
    if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
        logger.info(f"Using cached MP3 file: {mp3_path}")
        return mp3_path, video_id
    
    # Check if this download is already in progress
    with active_downloads_lock:
        if video_id in active_downloads:
            # Wait for a bit and check if it's completed
            logger.info(f"Download already in progress for video ID: {video_id}, waiting...")
            for _ in range(10):  # Wait for max 5 seconds
                time.sleep(0.5)
                if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
                    logger.info(f"Downloaded file is now available: {mp3_path}")
                    return mp3_path, video_id
            
            # If still not available, consider it a new request
            logger.info(f"Timed out waiting for active download, proceeding with new request")
        
        # Mark this download as in progress
        active_downloads[video_id] = True
    
    try:
        # List of methods to try in order
        methods = [
            download_tiktok_video_mobile,
            download_tiktok_video_scraper,  # Added new method
            download_tiktok_video_web,
            download_tiktok_video_embed
        ]
        
        video_path = None
        for method in methods:
            try:
                logger.info(f"Trying download method: {method.__name__}")
                video_path = method(video_id)
                
                if video_path:
                    logger.info(f"Successfully downloaded video using {method.__name__}")
                    
                    # Convert video to MP3 with specified quality
                    mp3_path = convert_video_to_mp3(video_path, video_id, quality)
                    if mp3_path:
                        return mp3_path, video_id
                        
                # Wait a moment before trying the next method to avoid rate limiting
                time.sleep(random.uniform(0.5, 1.5))
            except Exception as e:
                logger.error(f"Error in download method {method.__name__}: {e}")
        
        logger.error("All download methods failed")
        return None, video_id
    finally:
        # Remove the video ID from active downloads
        with active_downloads_lock:
            if video_id in active_downloads:
                del active_downloads[video_id]

def cleanup_old_files():
    """Clean up old temporary files to prevent disk space issues."""
    try:
        current_time = time.time()
        files_to_delete = []
        total_size = 0
        
        # First calculate total size and sort files by age
        files_info = []
        for filename in os.listdir(TEMP_DIR):
            file_path = os.path.join(TEMP_DIR, filename)
            if os.path.isfile(file_path):
                file_size = os.path.getsize(file_path)
                file_mtime = os.path.getmtime(file_path)
                files_info.append((file_path, file_size, file_mtime))
                total_size += file_size
        
        # Sort files by modification time (oldest first)
        files_info.sort(key=lambda x: x[2])
        
        # If total size exceeds MAX_CACHE_SIZE_MB, delete oldest files first
        if total_size > MAX_CACHE_SIZE_MB * 1024 * 1024:
            logger.info(f"Cache size ({total_size/(1024*1024):.2f} MB) exceeds limit ({MAX_CACHE_SIZE_MB} MB). Cleaning up...")
            
            for file_path, file_size, file_mtime in files_info:
                os.remove(file_path)
                logger.info(f"Removed file to reduce cache size: {os.path.basename(file_path)}")
                total_size -= file_size
                if total_size <= MAX_CACHE_SIZE_MB * 0.9 * 1024 * 1024:  # Clean until we're under 90% of max
                    break
        
        # Now delete expired files
        for file_path, file_size, file_mtime in files_info:
            if current_time - file_mtime > CACHE_EXPIRATION:
                os.remove(file_path)
                logger.info(f"Removed expired file: {os.path.basename(file_path)}")
    except Exception as e:
        logger.error(f"Error cleaning up old files: {e}")

def validate_input(data):
    """Validate and sanitize incoming request data."""
    if not data or not isinstance(data, dict):
        return False, {"error": "Invalid JSON data"}, 400
        
    url = data.get('url', '').strip()
    if not url:
        return False, {"error": "No URL provided"}, 400
    
    if not is_valid_tiktok_url(url):
        return False, {"error": "Invalid TikTok URL"}, 400
    
    # Validate quality parameter
    quality = data.get('quality', '192').strip()
    if quality not in ['128', '192', '256', '320']:
        quality = '192'  # Default to 192 kbps if invalid
    
    # Validate turnstile token if required
    if app.config.get('REQUIRE_TURNSTILE', False):
        token = data.get('turnstile_token')
        if not token:
            return False, {"error": "Turnstile verification required"}, 403
    
    return True, {"url": url, "quality": quality, "turnstile_token": data.get('turnstile_token')}, 200

@app.before_request
def before_request():
    """Add security headers to all responses."""
    # Clean up files occasionally to prevent disk space issues
    if random.random() < 0.05:  # 5% chance to trigger cleanup
        threading.Thread(target=cleanup_old_files).start()

@app.after_request
def add_security_headers(response):
    """Add security headers to response."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

@app.errorhandler(Exception)
def handle_error(e):
    """Global error handler."""
    logger.error(f"Unhandled exception: {str(e)}")
    return jsonify({"error": "An unexpected error occurred. Please try again later."}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint to verify service is running."""
    return jsonify({
        "status": "ok", 
        "message": "TikTok to MP3 converter service is running",
        "version": "2.0.0",
        "timestamp": datetime.now().isoformat()
    })

@app.route('/api/convert', methods=['POST'])
def convert_tiktok_to_mp3():
    """API endpoint to download TikTok videos and convert to MP3."""
    try:
        # Rate limiting check
        if not can_perform_download():
            return jsonify({"error": "Rate limit exceeded. Please try again later."}), 429
            
        # Parse and validate input
        data = request.json
        is_valid, result, status_code = validate_input(data)
        
        if not is_valid:
            return jsonify(result), status_code
            
        url = result["url"]
        quality = result["quality"]
        turnstile_token = result["turnstile_token"]
        
        # Verify Cloudflare Turnstile token if enabled
        if app.config.get('REQUIRE_TURNSTILE', False) and turnstile_token:
            is_valid, error_codes = verify_turnstile_token(
                turnstile_token, 
                request.remote_addr
            )
            
            if not is_valid:
                logger.warning(f"Turnstile verification failed: {error_codes}")
                return jsonify({"error": "Turnstile verification failed", "details": error_codes}), 403
        
        # Update download statistics
        stats.increment_total()
        
        # Try to download the video and convert to MP3
        mp3_path, video_id = get_tiktok_video(url, quality)
        
        if not mp3_path:
            stats.increment_failed()
            return jsonify({"error": "Failed to download and convert video"}), 500
            
        # Successfully downloaded and converted
        stats.increment_success()
        
        # Set appropriate headers for download
        return send_file(
            mp3_path, 
            as_attachment=True, 
            download_name=f"tiktok_{video_id}_{quality}kbps.mp3",
            mimetype="audio/mpeg"
        )
        
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """API endpoint to get service statistics."""
    try:
        # Count files by extension
        file_counts = {
            "mp3_files": 0,
            "mp4_files": 0,
            "other_files": 0
        }
        
        total_size = 0
        oldest_file_time = time.time()
        newest_file_time = 0
        
        for f in os.listdir(TEMP_DIR):
            file_path = os.path.join(TEMP_DIR, f)
            if not os.path.isfile(file_path):
                continue
                
            # Update file counts
            if f.endswith('.mp3'):
                file_counts["mp3_files"] += 1
            elif f.endswith('.mp4'):
                file_counts["mp4_files"] += 1
            else:
                file_counts["other_files"] += 1
            
            # Update total size
            file_size = os.path.getsize(file_path)
            total_size += file_size
            
            # Update file time stats
            file_time = os.path.getmtime(file_path)
            oldest_file_time = min(oldest_file_time, file_time)
            newest_file_time = max(newest_file_time, file_time)
        
        # Calculate cache age in hours
        cache_age = {
            "oldest_file_hours": round((time.time() - oldest_file_time) / 3600, 2) if oldest_file_time < time.time() else 0,
            "newest_file_hours": round((time.time() - newest_file_time) / 3600, 2) if newest_file_time > 0 else 0
        }
        
        # Get download statistics
        download_stats = stats.get_stats()
        
        # System information
        system_info = {
            "temp_dir": TEMP_DIR,
            "free_space_mb": shutil.disk_usage(TEMP_DIR).free / (1024 * 1024),
            "total_space_mb": shutil.disk_usage(TEMP_DIR).total / (1024 * 1024),
            "active_downloads": len(active_downloads)
        }
        
        return jsonify({
            "status": "ok",
            "stats": {
                "files": file_counts,
                "total_files": sum(file_counts.values()),
                "cache_dir_size_mb": round(total_size / (1024 * 1024), 2),
                "cache_age": cache_age,
                "downloads": download_stats,
                "system": system_info
            }
        })
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return jsonify({"error": f"Error getting stats: {str(e)}"}), 500

@app.route('/api/clear-cache', methods=['POST'])
def clear_cache():
    """Endpoint to clear the cache (requires admin secret)."""
    try:
        # Simple admin authentication
        admin_secret = request.headers.get('X-Admin-Secret')
        if not admin_secret or admin_secret != os.environ.get('ADMIN_SECRET', 'change_this_secret'):
            return jsonify({"error": "Unauthorized"}), 401
            
        # Clear the cache
        files_removed = 0
        for filename in os.listdir(TEMP_DIR):
            file_path = os.path.join(TEMP_DIR, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
                files_removed += 1
                
        # Clear the function caches
        download_tiktok_video_mobile.cache_clear()
        download_tiktok_video_web.cache_clear()
        download_tiktok_video_embed.cache_clear()
        download_tiktok_video_scraper.cache_clear()
        
        return jsonify({
            "status": "ok",
            "message": f"Cache cleared successfully. Removed {files_removed} files."
        })
    except Exception as e:
        logger.error(f"Error clearing cache: {e}")
        return jsonify({"error": f"Error clearing cache: {str(e)}"}), 500

def configure_app():
    """Configure the Flask app with environment variables."""
    # Turnstile configuration
    app.config['REQUIRE_TURNSTILE'] = os.environ.get('REQUIRE_TURNSTILE', 'false').lower() == 'true'
    
    # Cache configuration
    global CACHE_EXPIRATION, MAX_CACHE_SIZE_MB
    CACHE_EXPIRATION = int(os.environ.get('CACHE_EXPIRATION_SECONDS', CACHE_EXPIRATION))
    MAX_CACHE_SIZE_MB = int(os.environ.get('MAX_CACHE_SIZE_MB', MAX_CACHE_SIZE_MB))
    
    # Rate limiting configuration
    global MAX_DOWNLOADS_PER_MINUTE
    MAX_DOWNLOADS_PER_MINUTE = int(os.environ.get('MAX_DOWNLOADS_PER_MINUTE', MAX_DOWNLOADS_PER_MINUTE))
    
    # Configure proxies if provided
    global PROXIES
    proxy_url = os.environ.get('HTTP_PROXY')
    if proxy_url:
        PROXIES = {
            "http": proxy_url,
            "https": proxy_url
        }
    
    logger.info("Application configured successfully")
    logger.info(f"Turnstile verification required: {app.config['REQUIRE_TURNSTILE']}")
    logger.info(f"Cache expiration: {CACHE_EXPIRATION} seconds")
    logger.info(f"Max cache size: {MAX_CACHE_SIZE_MB} MB")
    logger.info(f"Rate limit: {MAX_DOWNLOADS_PER_MINUTE} downloads per minute")
    logger.info(f"Using proxies: {PROXIES is not None}")

if __name__ == '__main__':
    # Create a README file for Render deployment
    readme_path = 'README.md'
    with open(readme_path, 'w') as f:
        f.write("""# TikTok to MP3 Converter API

A Flask-based API service that downloads TikTok videos and converts them to MP3 format.

## Features
- Download TikTok videos using multiple fallback methods
- Convert videos to MP3 with customizable quality (128, 192, 256, or 320 kbps)
- Cloudflare Turnstile protection to prevent abuse
- Rate limiting and cache management
- Multiple download methods with fallback for reliability

## Requirements
- Python 3.8+
- FFmpeg must be installed on the system

## Environment Variables
- `REQUIRE_TURNSTILE`: Set to 'true' to enable Cloudflare Turnstile verification (default: false)
- `TURNSTILE_SECRET_KEY`: Your Cloudflare Turnstile secret key
- `CACHE_EXPIRATION_SECONDS`: Time in seconds before cached files expire (default: 86400)
- `MAX_CACHE_SIZE_MB`: Maximum cache size in MB (default: 5000)
- `MAX_DOWNLOADS_PER_MINUTE`: Rate limit for downloads (default: 20)
- `ADMIN_SECRET`: Secret key for admin operations like cache clearing
- `HTTP_PROXY`: Optional proxy URL for outgoing requests

## API Endpoints
- POST /api/convert: Convert TikTok video to MP3
- GET /api/health: Health check endpoint
- GET /api/stats: Get service statistics
- POST /api/clear-cache: Clear the cache (requires admin authentication)

## Deployment
This service is designed to be deployed on Render.com.

### Render.com Setup
1. Create a new Web Service
2. Use the Docker Runtime
3. Set the required environment variables
4. Make sure to install FFmpeg in your build script
""")

    # Create a Dockerfile for Render deployment
    dockerfile_path = 'Dockerfile'
    with open(dockerfile_path, 'w') as f:
        f.write("""FROM python:3.10-slim

WORKDIR /app

# Install FFmpeg and other dependencies
RUN apt-get update && \\
    apt-get install -y ffmpeg curl && \\
    apt-get clean && \\
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create log directory
RUN mkdir -p /var/log/tiktok_converter

# Expose port
EXPOSE 8080

# Command to run the application
CMD gunicorn --bind 0.0.0.0:$PORT --workers 4 --threads 2 --timeout 120 app:app
""")

    # Create requirements.txt for dependencies
    requirements_path = 'requirements.txt'
    with open(requirements_path, 'w') as f:
        f.write("""flask==2.3.3
flask-cors==4.0.0
requests==2.31.0
gunicorn==21.2.0
""")

    # Configure the application
    configure_app()

    print("TikTok to MP3 Converter API Server 2.0")
    print("----------------------------")
    print("API Endpoints:")
    print("  - POST /api/convert: Convert TikTok videos to MP3")
    print("  - GET /api/health: Health check endpoint")
    print("  - GET /api/stats: Get service statistics")
    print("  - POST /api/clear-cache: Clear the cache (requires admin authentication)")
    print("\nServer is starting on http://0.0.0.0:8080\n")
    
    # Run the Flask app with gunicorn configuration for production
    port = int(os.environ.get("PORT", 8080))
    app.run(
        host='0.0.0.0',  # Allow external connections for production
        port=port,
        debug=False,
        threaded=True
    )
