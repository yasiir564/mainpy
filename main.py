import requests
import re
import json
import random
import time
import logging
from flask import Flask, request, Response, jsonify, send_file
from urllib.parse import urlencode, urlparse
import io

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('tiktok_downloader')

app = Flask(__name__)

# Proxy management
class ProxyManager:
    def __init__(self):
        self.proxies = []
        self.last_refresh = 0
        self.refresh_interval = 300  # 5 minutes
        self.failed_proxies = set()
        
    def get_proxy(self):
        """Get a working proxy, refreshing the list if needed"""
        current_time = time.time()
        
        # Refresh proxies if interval passed or list is empty
        if current_time - self.last_refresh > self.refresh_interval or not self.proxies:
            self.refresh_proxies()
            
        # If we have proxies after refresh, return one
        if self.proxies:
            return random.choice(self.proxies)
        else:
            logger.warning("No proxies available")
            return None
    
    def refresh_proxies(self):
        """Refresh the proxy list from multiple sources"""
        logger.info("Refreshing proxy list")
        self.proxies = []
        
        # Try multiple proxy sources
        sources = [
            self.get_from_gimmeproxy,
            self.get_from_proxylist,
            self.get_from_freeproxy
        ]
        
        for source in sources:
            try:
                new_proxies = source()
                if new_proxies:
                    self.proxies.extend(new_proxies)
                    logger.info(f"Added {len(new_proxies)} proxies from {source.__name__}")
            except Exception as e:
                logger.error(f"Error getting proxies from {source.__name__}: {str(e)}")
        
        # Remove failed proxies from the list
        self.proxies = [p for p in self.proxies if p not in self.failed_proxies]
        logger.info(f"Total valid proxies after refresh: {len(self.proxies)}")
        
        self.last_refresh = time.time()
    
    def get_from_gimmeproxy(self):
        """Get proxy from GimmeProxy API"""
        try:
            url = 'https://gimmeproxy.com/api/getProxy?supportsHttps=true&protocol=http'
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data and 'ip' in data and 'port' in data:
                    proxy = f"{data['ip']}:{data['port']}"
                    return [proxy]
            return []
        except Exception as e:
            logger.error(f"GimmeProxy error: {str(e)}")
            return []
    
    def get_from_proxylist(self):
        """Get proxies from free-proxy-list.net"""
        try:
            url = 'https://www.free-proxy-list.net/'
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                # Extract proxies using regex
                pattern = r'<td>(\d+\.\d+\.\d+\.\d+)</td><td>(\d+)</td>'
                matches = re.findall(pattern, response.text)
                
                proxies = []
                for ip, port in matches:
                    proxies.append(f"{ip}:{port}")
                return proxies[:20]  # Limit to 20 proxies
            return []
        except Exception as e:
            logger.error(f"Free-proxy-list error: {str(e)}")
            return []
    
    def get_from_freeproxy(self):
        """Get proxies from geonode free proxy list"""
        try:
            url = 'https://proxylist.geonode.com/api/proxy-list?limit=50&page=1&sort_by=lastChecked&sort_type=desc'
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                proxies = []
                for proxy in data.get('data', []):
                    if 'ip' in proxy and 'port' in proxy:
                        proxies.append(f"{proxy['ip']}:{proxy['port']}")
                return proxies
            return []
        except Exception as e:
            logger.error(f"GeoNode proxy error: {str(e)}")
            return []
    
    def mark_failed(self, proxy):
        """Mark a proxy as failed"""
        if proxy:
            self.failed_proxies.add(proxy)
            if proxy in self.proxies:
                self.proxies.remove(proxy)

# Initialize the proxy manager
proxy_manager = ProxyManager()

def make_request(url, headers=None, use_proxy=True, method='GET', data=None, stream=False, allow_redirects=True):
    """Make a request with proxy rotation and error handling"""
    if headers is None:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://www.tiktok.com/',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9'
        }
    
    # Try up to 3 proxies
    max_attempts = 3 if use_proxy else 1
    
    for attempt in range(max_attempts):
        # For first attempts, use proxy if requested
        current_proxy = None
        proxies = None
        
        if use_proxy and attempt < max_attempts - 1:
            current_proxy = proxy_manager.get_proxy()
            if current_proxy:
                proxies = {
                    'http': f'http://{current_proxy}',
                    'https': f'http://{current_proxy}'
                }
                logger.info(f"Using proxy: {current_proxy} for {url}")
        
        try:
            if method.upper() == 'GET':
                response = requests.get(
                    url, 
                    headers=headers, 
                    proxies=proxies, 
                    timeout=30,
                    stream=stream,
                    allow_redirects=allow_redirects
                )
            elif method.upper() == 'POST':
                response = requests.post(
                    url, 
                    headers=headers, 
                    proxies=proxies, 
                    data=data, 
                    timeout=30,
                    allow_redirects=allow_redirects
                )
            elif method.upper() == 'HEAD':
                response = requests.head(
                    url, 
                    headers=headers, 
                    proxies=proxies, 
                    timeout=15,
                    allow_redirects=allow_redirects
                )
            
            # Check if successful
            if response.status_code < 400:
                logger.info(f"Request successful: {url} (Status: {response.status_code})")
                return response
            
            logger.warning(f"Request failed: {url} (Status: {response.status_code}, Proxy: {current_proxy})")
            
            # If proxy caused the issue, mark it as failed
            if current_proxy:
                proxy_manager.mark_failed(current_proxy)
                
        except Exception as e:
            logger.error(f"Request error for {url}: {str(e)}")
            if current_proxy:
                proxy_manager.mark_failed(current_proxy)
    
    # Last resort: no proxy
    try:
        logger.info(f"Last attempt without proxy: {url}")
        if method.upper() == 'GET':
            response = requests.get(url, headers=headers, timeout=30, stream=stream, allow_redirects=allow_redirects)
        elif method.upper() == 'POST':
            response = requests.post(url, headers=headers, data=data, timeout=30, allow_redirects=allow_redirects)
        elif method.upper() == 'HEAD':
            response = requests.head(url, headers=headers, timeout=15, allow_redirects=allow_redirects)
        
        return response
    except Exception as e:
        logger.error(f"Final request error for {url}: {str(e)}")
        return None

# TikTok video extraction functions
def extract_tiktok_id(url):
    """Extract TikTok video ID from URL"""
    # Normalize URL
    normalized_url = url.replace('m.tiktok.com', 'www.tiktok.com')
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
    
    # If nothing matched, try with original URL
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    # For vm.tiktok.com and other short URLs - always follow redirect
    parsed_url = urlparse(url)
    if ('vm.tiktok.com' in url or 
        'vt.tiktok.com' in url or 
        len(parsed_url.netloc) < 15):  # Short domain names likely redirects
        return 'follow_redirect'
    
    return None

def follow_tiktok_redirects(url):
    """Follow redirects for TikTok short URLs"""
    logger.info(f"Following redirects for: {url}")
    
    response = make_request(url, method='HEAD')
    
    if response and 200 <= response.status_code < 400:
        return response.url
    
    return url  # Return original URL if redirect failed

def get_tiktok_video_direct(url):
    """Try to extract TikTok video info directly from the HTML"""
    logger.info(f"Trying direct HTML method for: {url}")
    
    headers = {
        'Referer': 'https://www.tiktok.com/',
        'Origin': 'https://www.tiktok.com',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9'
    }
    
    response = make_request(url, headers=headers)
    
    if not response or response.status_code != 200:
        logger.error(f"Failed to fetch HTML content, status code: {response.status_code if response else 'None'}")
        return None
    
    html = response.text
    
    # Process HTML to extract video data
    result = {}
    
    # Try to extract username
    match = re.search(r'"uniqueId"\s*:\s*"([^"]+)"', html)
    if match:
        result['author'] = match.group(1)
    else:
        result['author'] = 'Unknown'
    
    # Try to extract description
    match = re.search(r'"desc"\s*:\s*"([^"]*)"', html)
    if match:
        result['desc'] = match.group(1).replace('\\', '')
    else:
        result['desc'] = ''
    
    # Try to extract video ID
    match = re.search(r'"id"\s*:\s*"(\d+)"', html)
    if match:
        result['video_id'] = match.group(1)
    else:
        # Try alternative pattern
        match = re.search(r'video\/(\d+)', url)
        if match:
            result['video_id'] = match.group(1)
        else:
            import hashlib
            result['video_id'] = hashlib.md5(url.encode()).hexdigest()  # Fallback to a hash of the URL
    
    # Try to extract video URL
    match = re.search(r'"playAddr"\s*:\s*"([^"]+)"', html)
    if match:
        result['video_url'] = match.group(1).replace('\\u002F', '/')
    else:
        match = re.search(r'"downloadAddr"\s*:\s*"([^"]+)"', html)
        if match:
            result['video_url'] = match.group(1).replace('\\u002F', '/')
        else:
            match = re.search(r'\<video[^>]+src="([^"]+)"', html)
            if match:
                result['video_url'] = match.group(1)
            else:
                logger.error('Failed to extract video URL from HTML')
                return None
    
    # Try to extract cover image
    match = re.search(r'"cover"\s*:\s*"([^"]+)"', html)
    if match:
        result['cover_url'] = match.group(1).replace('\\u002F', '/')
    else:
        result['cover_url'] = ''
    
    return result

def fetch_from_ttdownloader(url):
    """Fetch video from TTDownloader service"""
    logger.info(f"Trying TTDownloader service for: {url}")
    
    # First step: get the token
    response = make_request('https://ttdownloader.com/')
    
    if not response or response.status_code != 200:
        logger.error('Failed to access TTDownloader service')
        return None
    
    html = response.text
    
    # Extract token
    match = re.search(r'name="([^"]+)" value="([^"]+)"', html)
    if not match:
        logger.error('Failed to extract token from TTDownloader')
        return None
    
    token_name = match.group(1)
    token_value = match.group(2)
    
    # Second step: submit the URL and token
    post_data = {
        'url': url,
        token_name: token_value
    }
    
    response = make_request('https://ttdownloader.com/req/', method='POST', data=post_data)
    
    if not response or response.status_code != 200:
        logger.error('Failed to get a response from TTDownloader API')
        return None
    
    response_text = response.text
    
    # Extract the download link (without watermark)
    match = re.search(r'id="download-link"[^>]+href="([^"]+)"', response_text)
    if match:
        video_url = match.group(1)
        
        # Extract username if available
        author = 'Unknown'
        match = re.search(r'@([a-zA-Z0-9_.]+)', url)
        if match:
            author = match.group(1)
        
        import hashlib
        return {
            'video_url': video_url,
            'author': author,
            'desc': 'Video from TikTok',
            'video_id': hashlib.md5(url.encode()).hexdigest(),
            'cover_url': ''
        }
    
    logger.error('Failed to extract download link from TTDownloader response')
    return None

# Flask routes
@app.route('/api/tiktok/download', methods=['POST', 'OPTIONS'])
def download_tiktok():
    """API endpoint to download TikTok video"""
    # Handle CORS
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()
    
    # Get request body
    try:
        data = request.get_json()
        logger.info(f"Download request received: {data}")
    except Exception as e:
        logger.error(f"Invalid JSON: {str(e)}")
        return jsonify({"success": False, "error": "Invalid JSON data"}), 400
    
    # Validate URL parameter
    if not data or 'url' not in data or not data['url']:
        logger.error('Error: TikTok URL is missing')
        return jsonify({
            'success': False, 
            'error': 'TikTok URL is required.'
        }), 400
    
    tiktok_url = data['url'].strip()
    logger.info(f'TikTok URL received: {tiktok_url}')
    
    # Main logic: Try multiple methods to get the video info
    
    # 1. First, try the direct HTML approach
    direct_result = get_tiktok_video_direct(tiktok_url)
    
    if direct_result and 'video_url' in direct_result:
        logger.info('Successfully extracted video info using direct method')
        
        # Test if direct access works
        test_request = make_request(direct_result['video_url'], method='HEAD')
        
        # If direct access fails, we'll need to proxy the video download
        if test_request and test_request.status_code == 403:
            logger.info('Direct video URL access blocked (403), setting proxy_needed flag')
            direct_result['original_video_url'] = direct_result['video_url']
            direct_result['video_url'] = f"/api/tiktok/proxy-download?url={direct_result['video_url']}"
            direct_result['proxy_used'] = True
        
        return jsonify({
            'success': True,
            'video_url': direct_result['video_url'],
            'cover_url': direct_result.get('cover_url', ''),
            'author': direct_result.get('author', 'Unknown'),
            'desc': direct_result.get('desc', ''),
            'video_id': direct_result.get('video_id', ''),
            'method': 'direct',
            'proxy_used': direct_result.get('proxy_used', False)
        }), 200
    
    # 2. Try the TTDownloader service as a fallback
    ttdownloader_result = fetch_from_ttdownloader(tiktok_url)
    
    if ttdownloader_result and 'video_url' in ttdownloader_result:
        logger.info('Successfully extracted video info using TTDownloader')
        
        return jsonify({
            'success': True,
            'video_url': ttdownloader_result['video_url'],
            'cover_url': ttdownloader_result.get('cover_url', ''),
            'author': ttdownloader_result.get('author', 'Unknown'),
            'desc': ttdownloader_result.get('desc', ''),
            'video_id': ttdownloader_result.get('video_id', ''),
            'method': 'ttdownloader'
        }), 200
    
    # 3. Original TikTok API approach as last resort
    # Extract video ID from URL
    video_id = extract_tiktok_id(tiktok_url)
    
    # Handle redirects for short URLs
    if video_id == 'follow_redirect' or (isinstance(video_id, str) and len(video_id) < 10):
        final_url = follow_tiktok_redirects(tiktok_url)
        logger.info(f'Followed URL redirect to: {final_url}')
        
        # Try to extract video ID from the final URL
        video_id = extract_tiktok_id(final_url)
        
        # If still no video ID, look for it in the HTML content
        if not video_id:
            logger.info('Trying to extract video ID from HTML content')
            
            response = make_request(final_url)
            
            if response and response.status_code == 200:
                html = response.text
                
                # Try to find video ID in the HTML
                match = re.search(r'itemId["\s:=]+["\']?(\d+)["\']?', html, re.IGNORECASE)
                if match:
                    video_id = match.group(1)
                    logger.info(f'Extracted video ID from HTML: {video_id}')
                else:
                    match = re.search(r'video[\/:](\d+)', final_url, re.IGNORECASE)
                    if match:
                        video_id = match.group(1)
                        logger.info(f'Extracted video ID from final URL: {video_id}')
    
    if not video_id:
        logger.error(f'Error: Could not extract video ID from URL: {tiktok_url}')
        return jsonify({
            'success': False, 
            'error': 'Could not extract video ID from the provided TikTok URL. Please try a different link format.'
        }), 400
    
    logger.info(f'Using video ID: {video_id}')
    
    # Build the TikTok web API URL
    api_url = f"https://www.tiktok.com/api/item/detail/?itemId={video_id}"
    logger.info(f'Calling TikTok API with proxy: {api_url}')
    
    # Set headers for TikTok API
    headers = {
        'Referer': 'https://www.tiktok.com/',
        'Origin': 'https://www.tiktok.com',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9'
    }
    
    # Make proxied request to TikTok API
    response = make_request(api_url, headers=headers)
    
    if not response or response.status_code != 200:
        logger.error(f'Error: TikTok API request failed with status: {response.status_code if response else "None"}')
        
        # Try one more time with a different proxy
        logger.info('Retrying with a different proxy')
        response = make_request(api_url, headers=headers)
        
        if not response or response.status_code != 200:
            logger.error('Error: Second TikTok API request also failed')
            
            # Try one more time with the direct HTML approach
            direct_result = get_tiktok_video_direct(tiktok_url)
            
            if direct_result and 'video_url' in direct_result:
                logger.info('Successfully extracted video info using direct method after API failed')
                
                return jsonify({
                    'success': True,
                    'video_url': direct_result['video_url'],
                    'cover_url': direct_result.get('cover_url', ''),
                    'author': direct_result.get('author', 'Unknown'),
                    'desc': direct_result.get('desc', ''),
                    'video_id': direct_result.get('video_id', ''),
                    'method': 'direct_fallback'
                }), 200
            
            return jsonify({
                'success': False, 
                'error': 'Failed to fetch video information from TikTok API. Please try a different link.'
            }), 500
    
    try:
        data = response.json()
    except json.JSONDecodeError as e:
        logger.error(f'Error parsing JSON: {str(e)}')
        logger.error(f'Response preview: {response.text[:500]}')
        return jsonify({
            'success': False, 
            'error': 'Failed to parse API response from TikTok. TikTok may have changed their API.'
        }), 500
    
    # Check for empty or invalid response structure
    if 'itemInfo' not in data or 'itemStruct' not in data['itemInfo']:
        logger.error('Error: Invalid API response structure')
        logger.error(f'Response structure: {list(data.keys())}')
        
        # Try one more time with the direct HTML approach
        direct_result = get_tiktok_video_direct(tiktok_url)
        
        if direct_result and 'video_url' in direct_result:
            logger.info('Successfully extracted video info using direct method after API failed')
            
            return jsonify({
                'success': True,
                'video_url': direct_result['video_url'],
                'cover_url': direct_result.get('cover_url', ''),
                'author': direct_result.get('author', 'Unknown'),
                'desc': direct_result.get('desc', ''),
                'video_id': direct_result.get('video_id', ''),
                'method': 'direct_fallback'
            }), 200
        
        return jsonify({
            'success': False, 
            'error': 'TikTok returned an invalid response structure. The video may be private or deleted.'
        }), 500
    
    # Try to find video URL in different possible locations
    video_url = None
    item_struct = data['itemInfo']['itemStruct']
    
    if 'video' in item_struct and 'playAddr' in item_struct['video']:
        video_url = item_struct['video']['playAddr']
    elif 'video' in item_struct and 'downloadAddr' in item_struct['video']:
        video_url = item_struct['video']['downloadAddr']
    elif 'video' in item_struct and 'urls' in item_struct['video'] and item_struct['video']['urls']:
        video_url = item_struct['video']['urls'][0]
    
    if not video_url:
        logger.error('Error: Video URL not found in API response')
        if 'video' in item_struct:
            logger.error(f'Video structure: {item_struct["video"]}')
        
        return jsonify({
            'success': False, 
            'error': 'Could not find video URL in TikTok response. Try a different video.'
        }), 500
    
    # Clean up the URL to remove watermark (if possible)
    video_url = video_url.replace('watermark=1', 'watermark=0')
    
    # Extract additional metadata
    author = item_struct.get('author', {}).get('uniqueId', 'Unknown')
    desc = item_struct.get('desc', '')
    cover_url = item_struct.get('video', {}).get('cover', '')
    
    # Test if direct access works
    test_request = make_request(video_url, method='HEAD')
    
    proxy_used = False
    
    # If direct access fails, we'll need to proxy the video download
    if test_request and test_request.status_code == 403:
        logger.info('Direct video URL access blocked (403), setting proxy_needed flag')
        original_video_url = video_url
        video_url = f"/api/tiktok/proxy-download?url={original_video_url}"
        proxy_used = True
    
    # Return the video information
    return jsonify({
        'success': True,
        'video_url': video_url,
        'cover_url': cover_url,
        'author': author,
        'desc': desc,
        'video_id': video_id,
        'method': 'api',
        'proxy_used': proxy_used
    }), 200

@app.route('/api/tiktok/proxy-download')
def proxy_download():
    """Proxy the video download to bypass restrictions"""
    url = request.args.get('url')
    
    if not url:
        return jsonify({'error': 'URL parameter is required'}), 400
    
    logger.info(f'Proxying video download for: {url}')
    
    # Headers to send with the download
    headers = {
        'Referer': 'https://www.tiktok.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    # Get video content with proxy
    response = make_request(url, headers=headers, use_proxy=True, stream=True)
    
    if not response or response.status_code != 200:
        logger.error(f'Proxy download failed with status code: {response.status_code if response else "None"}')
        return jsonify({'error': 'Failed to download video'}), 500
    
    # Get content type from response
    content_type = response.headers.get('Content-Type', 'video/mp4')
    
    # Create a file-like object from the response content
    file_data = io.BytesIO()
    for chunk in response.iter_content(chunk_size=8192):
        file_data.write(chunk)
    
    # Reset file pointer
    file_data.seek(0)
    
    # Return the file
    return send_file(
        file_data,
        mimetype=content_type,
        as_attachment=True,
        download_name="tiktok_video.mp4"
    )

def _build_cors_preflight_response():
    """Handle CORS preflight requests"""
    response = jsonify({})
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
    response.headers.add("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    return response

# Add CORS headers to all responses
@app.after_request
def add_cors_headers(response):
    """Add CORS headers to all responses"""
    response.headers.add("Access-Control-Allow-Origin", "*")
    return response

if __name__ == "__main__":
    # Make sure we have proxies at startup
    proxy_manager.refresh_proxies()
    app.run(host='0.0.0.0', port=5000, debug=True)
