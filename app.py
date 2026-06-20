"""
Instagram Reel Caption Capture - Web API for Render
Extracts clean captions from Instagram Reel URLs via API
Saves captions with URL mapping to Google Drive
"""

import asyncio
import re
import json
import logging
import sys
import os
import base64
import pickle
import io
import math
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict, field
from datetime import datetime
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from playwright.async_api import async_playwright
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('reel_captures.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Instagram Reel Caption Capture", version="3.0.0")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Google Drive Configuration
SCOPES = ['https://www.googleapis.com/auth/drive']
DRIVE_FOLDER_NAME = "Reel_Finder_Data"
SHARED_FILE_NAME = "shared_reels.json"
CAPTIONS_FILE_NAME = "captions_data.json"


def safe_int(value):
    """Safely convert to int, handling NaN and None"""
    try:
        if value is None:
            return 0
        if isinstance(value, float) and math.isnan(value):
            return 0
        if isinstance(value, float) and math.isinf(value):
            return 0
        return int(float(value))
    except (ValueError, TypeError):
        return 0


def safe_float(value):
    """Safely convert to float, handling NaN and None"""
    try:
        if value is None:
            return 0.0
        if isinstance(value, float) and math.isnan(value):
            return 0.0
        if isinstance(value, float) and math.isinf(value):
            return 0.0
        return float(value)
    except (ValueError, TypeError):
        return 0.0


@dataclass
class ReelCaptionData:
    """Data structure for captured reel caption"""
    url: str
    shortcode: str
    username: str
    caption: str
    full_caption: str
    likes: int
    comments: int
    views: int
    timestamp: str
    hashtags: List[str] = field(default_factory=list)
    mentioned_users: List[str] = field(default_factory=list)
    is_video: bool = True
    duration: int = 0
    topic: str = ""  # Added for topic tracking
    
    def to_dict(self):
        return {
            "url": str(self.url),
            "shortcode": str(self.shortcode),
            "username": str(self.username),
            "caption": str(self.caption),
            "full_caption": str(self.full_caption),
            "likes": safe_int(self.likes),
            "comments": safe_int(self.comments),
            "views": safe_int(self.views),
            "timestamp": str(self.timestamp),
            "hashtags": [str(h) for h in self.hashtags] if self.hashtags else [],
            "mentioned_users": [str(m) for m in self.mentioned_users] if self.mentioned_users else [],
            "is_video": bool(self.is_video),
            "duration": safe_int(self.duration),
            "topic": str(self.topic)
        }


class GoogleDriveManager:
    """Manage Google Drive operations for captions data"""
    
    def __init__(self):
        self.service = self._authenticate()
        self.folder_id = self._get_or_create_folder()
    
    def _authenticate(self):
        """Authenticate with Google Drive"""
        creds = None
        
        # 1. Try environment token (Render)
        token_json = os.environ.get('GOOGLE_DRIVE_TOKEN')
        if token_json:
            try:
                decoded_bytes = base64.b64decode(token_json)
                try:
                    creds = pickle.loads(decoded_bytes)
                except:
                    try:
                        token_data = json.loads(decoded_bytes.decode('utf-8'))
                        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
                    except:
                        token_data = json.loads(token_json)
                        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
                logger.info("✅ Drive authenticated via GOOGLE_DRIVE_TOKEN")
                return build('drive', 'v3', credentials=creds)
            except Exception as e:
                logger.warning(f"GOOGLE_DRIVE_TOKEN failed: {e}")
        
        # 2. Try local token file
        if os.path.exists('drive_token.pickle'):
            try:
                with open('drive_token.pickle', 'rb') as f:
                    creds = pickle.load(f)
                logger.info("✅ Drive authenticated via drive_token.pickle")
                return build('drive', 'v3', credentials=creds)
            except:
                pass
        
        # 3. Try credentials.json
        if os.path.exists('credentials.json'):
            try:
                from google_auth_oauthlib.flow import InstalledAppFlow
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
                logger.info("✅ Drive authenticated via credentials.json")
                with open('drive_token.pickle', 'wb') as f:
                    pickle.dump(creds, f)
                return build('drive', 'v3', credentials=creds)
            except:
                pass
        
        logger.error("❌ No Drive credentials found")
        return None
    
    def _get_or_create_folder(self):
        """Get or create the folder for captions data"""
        if not self.service:
            return None
        
        try:
            query = f"name='{DRIVE_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            results = self.service.files().list(q=query, fields="files(id)").execute()
            files = results.get('files', [])
            
            if files:
                logger.info(f"✅ Found folder: {DRIVE_FOLDER_NAME}")
                return files[0]['id']
            
            logger.info(f"📁 Creating folder: {DRIVE_FOLDER_NAME}")
            file_metadata = {
                'name': DRIVE_FOLDER_NAME,
                'mimeType': 'application/vnd.google-apps.folder'
            }
            folder = self.service.files().create(body=file_metadata, fields='id').execute()
            logger.info(f"✅ Created folder: {DRIVE_FOLDER_NAME}")
            return folder.get('id')
            
        except Exception as e:
            logger.error(f"Folder error: {e}")
            return None
    
    async def load_shared_data(self) -> Optional[Dict]:
        """Load shared data from Google Drive"""
        if not self.service or not self.folder_id:
            return None
        
        try:
            query = f"'{self.folder_id}' in parents and name='{SHARED_FILE_NAME}' and trashed=false"
            results = self.service.files().list(q=query, fields="files(id, name)").execute()
            files = results.get('files', [])
            
            if not files:
                logger.info(f"No shared file '{SHARED_FILE_NAME}' found")
                return None
            
            file_id = files[0]['id']
            logger.info(f"📥 Downloading: {SHARED_FILE_NAME}")
            
            request = self.service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    progress = int(status.progress() * 100)
                    logger.info(f"Download progress: {progress}%")
            
            fh.seek(0)
            data = json.loads(fh.read().decode('utf-8'))
            logger.info(f"✅ Loaded shared data with {data.get('total_urls', 0)} URLs")
            return data
            
        except Exception as e:
            logger.error(f"Load from Drive error: {e}")
            return None
    
    async def save_captions_data(self, captions: List[ReelCaptionData]) -> bool:
        """Save captions data to Google Drive"""
        if not self.service or not self.folder_id:
            logger.error("No Drive service available")
            return False
        
        try:
            # Prepare data
            data = {
                "timestamp": datetime.now().isoformat(),
                "total_captures": len(captions),
                "captions": [c.to_dict() for c in captions],
                "url_mapping": {c.url: c.shortcode for c in captions},
                "shortcode_mapping": {c.shortcode: c.url for c in captions},
                "source": "caption_capture",
                "last_updated": datetime.now().isoformat()
            }
            
            # Check if file exists
            query = f"'{self.folder_id}' in parents and name='{CAPTIONS_FILE_NAME}' and trashed=false"
            results = self.service.files().list(q=query, fields="files(id)").execute()
            files = results.get('files', [])
            
            # Create temporary file
            import tempfile
            file_content = json.dumps(data, indent=2, ensure_ascii=False)
            
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as temp_file:
                temp_file.write(file_content)
                temp_path = temp_file.name
            
            try:
                media = MediaFileUpload(
                    temp_path,
                    mimetype='application/json',
                    resumable=True
                )
                
                if files:
                    # Update existing file
                    file_id = files[0]['id']
                    logger.info(f"📤 Updating: {CAPTIONS_FILE_NAME}")
                    self.service.files().update(
                        fileId=file_id,
                        media_body=media
                    ).execute()
                    logger.info(f"✅ Updated captions file: {CAPTIONS_FILE_NAME}")
                else:
                    # Create new file
                    logger.info(f"📤 Creating: {CAPTIONS_FILE_NAME}")
                    file_metadata = {
                        'name': CAPTIONS_FILE_NAME,
                        'parents': [self.folder_id],
                        'description': f"Instagram Reel Captions - {datetime.now().strftime('%Y-%m-%d')}"
                    }
                    self.service.files().create(
                        body=file_metadata,
                        media_body=media
                    ).execute()
                    logger.info(f"✅ Created captions file: {CAPTIONS_FILE_NAME}")
                
                return True
                
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                    logger.debug(f"🗑️ Removed temp file: {temp_path}")
            
        except Exception as e:
            logger.error(f"Save to Drive error: {e}")
            return False
    
    async def load_captions_data(self) -> Optional[Dict]:
        """Load captions data from Google Drive"""
        if not self.service or not self.folder_id:
            return None
        
        try:
            query = f"'{self.folder_id}' in parents and name='{CAPTIONS_FILE_NAME}' and trashed=false"
            results = self.service.files().list(q=query, fields="files(id, name)").execute()
            files = results.get('files', [])
            
            if not files:
                logger.info(f"No captions file '{CAPTIONS_FILE_NAME}' found")
                return None
            
            file_id = files[0]['id']
            logger.info(f"📥 Downloading: {CAPTIONS_FILE_NAME}")
            
            request = self.service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    progress = int(status.progress() * 100)
                    logger.info(f"Download progress: {progress}%")
            
            fh.seek(0)
            data = json.loads(fh.read().decode('utf-8'))
            logger.info(f"✅ Loaded captions data with {data.get('total_captures', 0)} captions")
            return data
            
        except Exception as e:
            logger.error(f"Load from Drive error: {e}")
            return None































class InstagramCaptionExtractor:
    """Extract full captions from Instagram Reels"""
    
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.browser = None
        self.page = None
        self.playwright = None
        
    async def initialize(self):
        """Initialize browser"""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled'
            ]
        )
        self.page = await self.browser.new_page()
        await self.page.set_viewport_size({"width": 1280, "height": 800})
        
        await self.page.set_extra_http_headers({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        logger.info("✅ Browser initialized")
        
    async def close(self):
        """Close browser"""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("🔚 Browser closed")
    
    def extract_shortcode(self, url: str) -> Optional[str]:
        """Extract shortcode from Instagram URL"""
        patterns = [
            r'instagram\.com/reel/([A-Za-z0-9_-]+)',
            r'instagram\.com/p/([A-Za-z0-9_-]+)',
            r'instagram\.com/tv/([A-Za-z0-9_-]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None
    
    async def get_reel_caption(self, url: str, topic: str = "") -> Optional[ReelCaptionData]:
        """Get full caption from a single reel URL"""
        shortcode = self.extract_shortcode(url)
        if not shortcode:
            return None
        
        try:
            logger.info(f"📥 Fetching: {shortcode}")
            
            await self.page.goto(url, timeout=30000)
            await self.page.wait_for_load_state('networkidle', timeout=10000)
            await asyncio.sleep(3)
            
            data = await self.page.evaluate('''
                () => {
                    const result = {
                        username: '',
                        caption: '',
                        likes: 0,
                        comments: 0,
                        views: 0,
                        hashtags: [],
                        mentioned_users: [],
                        is_video: true,
                        duration: 0
                    };
                    
                    const usernameSelectors = [
                        'header h2',
                        'header a[href*="/"]',
                        'article header a',
                        'div._a9zr a',
                        'a._a9zr',
                        'span._a9zr a',
                        'div[class*="username"] a',
                        'h2[class*="username"]'
                    ];
                    
                    for (const selector of usernameSelectors) {
                        const el = document.querySelector(selector);
                        if (el) {
                            const username = el.textContent.trim();
                            if (username && username.length > 0 && !username.includes(' ')) {
                                result.username = username;
                                break;
                            }
                        }
                    }
                    
                    if (!result.username) {
                        const urlMatch = window.location.href.match(/\\/([A-Za-z0-9_.]+)\\/(?:reel|p|tv)\\//);
                        if (urlMatch) {
                            result.username = urlMatch[1];
                        }
                    }
                    
                    const captionSelectors = [
                        'div._a9zr',
                        'div.C4VMK span',
                        'div.weLOq span',
                        'h1._a9zr',
                        'div._a9zs',
                        'article div._a9zr',
                        'div[class*="caption"]',
                        'div[class*="Caption"]'
                    ];
                    
                    for (const selector of captionSelectors) {
                        const el = document.querySelector(selector);
                        if (el) {
                            const text = el.textContent.trim();
                            if (text && text.length > 10 && !text.startsWith('@')) {
                                result.caption = text;
                                break;
                            }
                        }
                    }
                    
                    if (!result.caption) {
                        const metaDesc = document.querySelector('meta[property="og:description"]');
                        if (metaDesc) {
                            result.caption = metaDesc.getAttribute('content') || '';
                        }
                    }
                    
                    if (result.caption) {
                        const hashtagMatches = result.caption.match(/#[A-Za-z0-9_]+/g);
                        if (hashtagMatches) {
                            result.hashtags = hashtagMatches.map(h => h.replace('#', ''));
                        }
                        
                        const mentionMatches = result.caption.match(/@[A-Za-z0-9_.]+/g);
                        if (mentionMatches) {
                            result.mentioned_users = mentionMatches.map(m => m.replace('@', ''));
                        }
                    }
                    
                    const likesMatch = document.body.textContent.match(/([\\d,.]+)\\s*(?:likes|❤️)/i);
                    if (likesMatch) {
                        try {
                            result.likes = parseInt(likesMatch[1].replace(/,/g, '')) || 0;
                        } catch (e) {
                            result.likes = 0;
                        }
                    }
                    
                    const commentsMatch = document.body.textContent.match(/([\\d,.]+)\\s*(?:comments|💬)/i);
                    if (commentsMatch) {
                        try {
                            result.comments = parseInt(commentsMatch[1].replace(/,/g, '')) || 0;
                        } catch (e) {
                            result.comments = 0;
                        }
                    }
                    
                    return result;
                }
            ''')
            
            if not data or not data.get('caption'):
                meta_caption = await self.page.evaluate('''
                    () => {
                        const meta = document.querySelector('meta[property="og:description"]');
                        return meta ? meta.getAttribute('content') : '';
                    }
                ''')
                if meta_caption:
                    data['caption'] = meta_caption
                    if not data.get('username'):
                        username_match = re.search(r'^([^:]+):', meta_caption)
                        if username_match:
                            data['username'] = username_match.group(1).strip()
            
            full_caption = data.get('caption', '')
            clean_caption = self._clean_caption(full_caption)
            
            if not clean_caption or clean_caption == 'No caption':
                clean_caption = full_caption
            
            username = data.get('username', 'unknown')
            username = username.lstrip('@').strip()
            
            if not username or username == 'unknown':
                url_parts = url.split('/')
                for part in url_parts:
                    if part and not part.startswith('http') and not part.startswith('www') and not part.startswith('@'):
                        if part not in ['reel', 'p', 'tv', '']:
                            username = part
                            break
            
            likes = data.get('likes', 0)
            comments = data.get('comments', 0)
            
            if likes == 0 and full_caption:
                likes_match = re.search(r'([\\d,.]+)\\s*(?:likes|❤️)', full_caption, re.IGNORECASE)
                if likes_match:
                    try:
                        likes_str = likes_match.group(1).replace(',', '')
                        likes = int(float(likes_str))
                    except (ValueError, TypeError):
                        likes = 0
            
            if comments == 0 and full_caption:
                comments_match = re.search(r'([\\d,.]+)\\s*(?:comments|💬)', full_caption, re.IGNORECASE)
                if comments_match:
                    try:
                        comments_str = comments_match.group(1).replace(',', '')
                        comments = int(float(comments_str))
                    except (ValueError, TypeError):
                        comments = 0
            
            return ReelCaptionData(
                url=url,
                shortcode=shortcode,
                username=username if username else 'unknown',
                caption=clean_caption,
                full_caption=full_caption,
                likes=safe_int(likes),
                comments=safe_int(comments),
                views=safe_int(data.get('views', 0)),
                timestamp=datetime.now().isoformat(),
                hashtags=data.get('hashtags', []),
                mentioned_users=data.get('mentioned_users', []),
                is_video=data.get('is_video', True),
                duration=safe_int(data.get('duration', 0)),
                topic=topic
            )
            
        except Exception as e:
            logger.error(f"❌ Error fetching {shortcode}: {e}")
            return None
    
    def _clean_caption(self, caption: str) -> str:
        """Clean the caption text - extract only the actual caption"""
        if not caption:
            return 'No caption'
        
        caption = re.sub(r'^📝\s*Caption:\s*', '', caption, flags=re.IGNORECASE)
        caption = re.sub(r'^Caption:\s*', '', caption, flags=re.IGNORECASE)
        caption = re.sub(r'^📝\s*', '', caption)
        caption = re.sub(r'^[A-Za-z0-9_.]+\s*:\s*', '', caption)
        caption = re.sub(r'https?://[^\s]+', '', caption)
        caption = re.sub(r'www\.[^\s]+', '', caption)
        caption = re.sub(r'👤 Uploader:.*?(?=\s|$)', '', caption)
        caption = re.sub(r'🏷️ Hashtags:.*?(?=\s|$)', '', caption)
        caption = re.sub(r'^[\d,.]+K?\s*(?:likes?|❤️)\s*,\s*[\d,.]+K?\s*(?:comments?|💬)\s*-\s*[A-Za-z0-9_.]+\s+on\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}:\s*', '', caption, flags=re.IGNORECASE)
        caption = re.sub(r'^[\d,.]+K?\s*(?:likes?|❤️)\s*,\s*[\d,.]+K?\s*(?:comments?|💬)\s*-\s*[A-Za-z0-9_.]+\s+on\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}\s*"', '"', caption, flags=re.IGNORECASE)
        caption = re.sub(r'^[\d,.]+K?\s*(?:likes?|❤️)\s*,\s*[\d,.]+K?\s*(?:comments?|💬)\s*-\s*[A-Za-z0-9_.]+\s+on\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}\s*', '', caption, flags=re.IGNORECASE)
        caption = re.sub(r'^"|"$', '', caption)
        caption = re.sub(r'\s+', ' ', caption).strip()
        
        if len(caption) < 10:
            quoted_match = re.search(r'"([^"]+)"', caption)
            if quoted_match:
                caption = quoted_match.group(1)
        
        caption = re.sub(r'[^\w\s.,!?\'"\-()#@]', '', caption)
        caption = re.sub(r'\s+', ' ', caption).strip()
        
        return caption if caption and len(caption) > 5 else 'No caption'
    
    async def get_multiple_captions(self, urls: List[str], topics: Dict[str, str] = None) -> List[ReelCaptionData]:
        """Get captions for multiple reel URLs with topic tracking"""
        results = []
        topics = topics or {}
        
        for url in urls:
            # Find topic for this URL
            topic = ""
            for t, topic_urls in topics.items():
                if url in topic_urls:
                    topic = t
                    break
            
            data = await self.get_reel_caption(url, topic)
            if data:
                results.append(data)
            await asyncio.sleep(2)
        return results










# Global instances
extractor = None
drive_manager = None

@app.on_event("startup")
async def startup_event():
    global extractor, drive_manager
    
    # Initialize Google Drive Manager
    drive_manager = GoogleDriveManager()
    logger.info("📁 Google Drive Manager initialized")
    
    # Initialize Caption Extractor
    extractor = InstagramCaptionExtractor(headless=True)
    await extractor.initialize()
    logger.info("🚀 Caption extractor initialized")

@app.on_event("shutdown")
async def shutdown_event():
    global extractor
    if extractor:
        await extractor.close()
        logger.info("🔚 Browser closed")


# -----------------------------
# Routes
# -----------------------------
@app.get("/")
async def root():
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Instagram Reel Caption Capture</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { background: #0a0a0f; color: #fff; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 20px; }
            .container { background: #1a1a1a; padding: 40px; border-radius: 16px; max-width: 900px; width: 100%; }
            h1 { color: #dc2743; font-size: 32px; margin-bottom: 10px; display: flex; align-items: center; gap: 10px; }
            .subtitle { color: #888; font-size: 16px; margin-bottom: 30px; }
            .drive-status {
                background: #2a2a2a;
                padding: 12px 15px;
                border-radius: 8px;
                margin-bottom: 20px;
                border-left: 3px solid #4ade80;
                font-size: 13px;
                color: #888;
            }
            .drive-status strong { color: #4ade80; }
            .input-section {
                background: #2a2a2a;
                padding: 20px;
                border-radius: 10px;
                margin-bottom: 20px;
            }
            textarea { 
                width: 100%; 
                padding: 15px; 
                background: #0a0a0a; 
                border: 2px solid #333; 
                border-radius: 10px; 
                color: #fff; 
                font-size: 14px; 
                font-family: monospace; 
                min-height: 100px; 
                resize: vertical; 
            }
            textarea:focus { outline: none; border-color: #dc2743; }
            .btn { 
                background: linear-gradient(135deg, #dc2743, #bc1888); 
                color: #fff; 
                border: none; 
                padding: 12px 24px; 
                border-radius: 10px; 
                font-size: 14px; 
                font-weight: 600; 
                cursor: pointer; 
                transition: all 0.3s; 
                margin: 5px; 
            }
            .btn:hover { transform: translateY(-2px); box-shadow: 0 4px 20px rgba(220, 39, 67, 0.4); }
            .btn-secondary { background: #333; }
            .btn-secondary:hover { background: #444; }
            .btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none !important; }
            .btn-group { display: flex; gap: 10px; flex-wrap: wrap; margin: 10px 0; }
            #results { margin-top: 20px; }
            .result-item { background: #2a2a2a; padding: 15px; border-radius: 10px; margin-bottom: 10px; border-left: 3px solid #dc2743; }
            .result-item .caption { color: #ddd; margin: 5px 0; }
            .result-item .meta { color: #888; font-size: 13px; }
            .result-item .hashtags { color: #4ade80; font-size: 13px; }
            .loading { text-align: center; padding: 20px; color: #888; }
            .error { color: #ff4444; }
            .endpoint-box { background: #0a0a0a; padding: 10px; border-radius: 8px; margin: 10px 0; font-family: monospace; color: #4ade80; font-size: 13px; overflow-x: auto; }
            .footer { margin-top: 20px; color: #666; font-size: 12px; text-align: center; border-top: 1px solid #2a2a2a; padding-top: 20px; }
            .stats { display: flex; gap: 20px; margin: 10px 0; flex-wrap: wrap; }
            .stat { background: #2a2a2a; padding: 8px 15px; border-radius: 8px; font-size: 13px; }
            .stat strong { color: #dc2743; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎬 Reel Caption Capture</h1>
            <p class="subtitle">Extract clean captions from Instagram Reels</p>
            
            <div class="drive-status">
                📁 Saving to: <strong>Google Drive (Reel_Finder_Data/captions_data.json)</strong>
                <br>📋 URLs from: <strong>Reel_Finder_Data/shared_reels.json</strong>
            </div>
            
            <div class="input-section">
                <h3 style="margin-bottom: 10px; color: #ccc; font-size: 14px;">📝 Enter URLs (one per line)</h3>
                <textarea id="urls" placeholder="https://www.instagram.com/reel/DZvVnIdxMla/&#10;https://www.instagram.com/reel/DZszmLkhAdY/"></textarea>
                
                <div class="btn-group">
                    <button class="btn" onclick="capture()" id="captureBtn">🔍 Capture & Save</button>
                    <button class="btn btn-secondary" onclick="loadFromDrive()">📥 Load URLs from Drive</button>
                    <button class="btn btn-secondary" onclick="clearResults()">🗑️ Clear</button>
                </div>
            </div>
            
            <div class="endpoint-box">
                POST /capture • GET /capture?urls=url1,url2 • GET /drive-urls • GET /captions
            </div>
            
            <div id="results"></div>
            
            <div class="footer">
                Powered by Playwright • Google Drive • FastAPI
            </div>
        </div>
        
        <script>
            async function capture() {
                const urlsText = document.getElementById('urls').value.trim();
                if (!urlsText) {
                    alert('Please enter at least one URL or load from Drive');
                    return;
                }
                
                const urls = urlsText.split('\\n').filter(u => u.trim());
                const btn = document.getElementById('captureBtn');
                const resultsDiv = document.getElementById('results');
                
                btn.disabled = true;
                btn.textContent = '⏳ Processing...';
                resultsDiv.innerHTML = '<div class="loading">⏳ Fetching captions...</div>';
                
                try {
                    const response = await fetch('/capture', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ urls: urls })
                    });
                    
                    const data = await response.json();
                    
                    if (data.error) {
                        resultsDiv.innerHTML = `<div class="error">❌ ${data.error}</div>`;
                        return;
                    }
                    
                    if (data.reels && data.reels.length > 0) {
                        let html = `
                            <div class="stats">
                                <span class="stat">📊 Total: <strong>${data.count}</strong> reels</span>
                                <span class="stat">📥 Captured: <strong>${data.captured}</strong></span>
                                <span class="stat">💾 Saved to Drive: <strong>✅</strong></span>
                            </div>
                        `;
                        
                        data.reels.forEach((reel, i) => {
                            const hashtags = reel.hashtags ? reel.hashtags.map(h => '#' + h).join(' ') : '';
                            html += `
                                <div class="result-item">
                                    <div><strong>#${i + 1}</strong> @${reel.username}</div>
                                    <div class="caption">${reel.caption}</div>
                                    <div class="meta">❤️ ${reel.likes} • 💬 ${reel.comments} • 🔗 <a href="${reel.url}" target="_blank" style="color:#4ade80;">${reel.shortcode}</a></div>
                                    ${reel.topic ? `<div class="meta">📂 Topic: #${reel.topic}</div>` : ''}
                                    ${hashtags ? `<div class="hashtags">${hashtags}</div>` : ''}
                                </div>
                            `;
                        });
                        
                        resultsDiv.innerHTML = html;
                    } else {
                        resultsDiv.innerHTML = '<div class="error">❌ No captions captured</div>';
                    }
                } catch (error) {
                    resultsDiv.innerHTML = `<div class="error">❌ Error: ${error.message}</div>`;
                } finally {
                    btn.disabled = false;
                    btn.textContent = '🔍 Capture & Save';
                }
            }
            
            async function loadFromDrive() {
                const resultsDiv = document.getElementById('results');
                resultsDiv.innerHTML = '<div class="loading">⏳ Loading URLs from Google Drive...</div>';
                
                try {
                    const response = await fetch('/drive-urls');
                    const data = await response.json();
                    
                    if (data.urls && data.urls.length > 0) {
                        document.getElementById('urls').value = data.urls.join('\\n');
                        resultsDiv.innerHTML = `<div class="stat" style="background: #10b98120; color: #4ade80; padding: 15px; border-radius: 10px;">
                            ✅ Loaded <strong>${data.count}</strong> URLs from Google Drive
                            <br><span style="font-size: 12px; color: #888;">Topics: ${data.topics.join(', ')}</span>
                        </div>`;
                    } else {
                        resultsDiv.innerHTML = '<div class="error">❌ No URLs found in Google Drive</div>';
                    }
                } catch (error) {
                    resultsDiv.innerHTML = `<div class="error">❌ Error loading from Drive: ${error.message}</div>`;
                }
            }
            
            function clearResults() {
                document.getElementById('results').innerHTML = '';
            }
        </script>
    </body>
    </html>
    """)

@app.post("/capture")
async def capture_captions(data: dict):
    """Capture captions from multiple reel URLs and save to Drive"""
    global extractor, drive_manager
    
    if not extractor:
        raise HTTPException(status_code=503, detail="Extractor not initialized")
    
    if not drive_manager:
        raise HTTPException(status_code=503, detail="Drive manager not initialized")
    
    urls = data.get("urls", [])
    
    if not urls:
        raise HTTPException(status_code=400, detail="No URLs provided")
    
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.split(',') if u.strip()]
    
    if not urls:
        raise HTTPException(status_code=400, detail="No valid URLs provided")
    
    try:
        # Get topics from shared data
        shared_data = await drive_manager.load_shared_data()
        topics_map = {}
        if shared_data and shared_data.get("topics"):
            for topic, reels in shared_data["topics"].items():
                for reel in reels:
                    if reel.get("url"):
                        topics_map[reel["url"]] = topic
        
        # Capture captions
        results = await extractor.get_multiple_captions(urls, topics_map)
        
        # Save to Google Drive
        save_success = await drive_manager.save_captions_data(results)
        
        # Ensure all values are JSON serializable
        serializable_results = []
        for r in results:
            try:
                serializable_results.append(r.to_dict())
            except Exception as e:
                logger.warning(f"Error serializing reel {r.shortcode}: {e}")
                serializable_results.append({
                    "url": str(r.url),
                    "shortcode": str(r.shortcode),
                    "username": str(r.username),
                    "caption": str(r.caption),
                    "full_caption": str(r.full_caption),
                    "likes": safe_int(r.likes),
                    "comments": safe_int(r.comments),
                    "views": safe_int(r.views),
                    "timestamp": str(r.timestamp),
                    "hashtags": [str(h) for h in r.hashtags] if r.hashtags else [],
                    "mentioned_users": [str(m) for m in r.mentioned_users] if r.mentioned_users else [],
                    "is_video": bool(r.is_video),
                    "duration": safe_int(r.duration),
                    "topic": str(r.topic)
                })
        
        return {
            "success": True,
            "count": len(serializable_results),
            "captured": len(serializable_results),
            "saved_to_drive": save_success,
            "reels": serializable_results,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Capture error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/capture")
async def capture_captions_get(urls: str = Query(..., description="Comma-separated reel URLs")):
    """Capture captions from multiple reel URLs (GET method)"""
    url_list = [u.strip() for u in urls.split(',') if u.strip()]
    return await capture_captions({"urls": url_list})

@app.get("/drive-urls")
async def get_drive_urls():
    """Get all URLs from Google Drive shared file"""
    global drive_manager
    
    if not drive_manager:
        raise HTTPException(status_code=503, detail="Drive manager not initialized")
    
    try:
        shared_data = await drive_manager.load_shared_data()
        
        if not shared_data:
            return {"success": False, "message": "No shared data found"}
        
        urls = []
        topics = shared_data.get("topics", {})
        for topic, reels in topics.items():
            for reel in reels:
                if isinstance(reel, dict) and reel.get("url"):
                    urls.append(reel["url"])
        
        return {
            "success": True,
            "count": len(urls),
            "urls": urls,
            "topics": list(topics.keys()),
            "source": "google_drive",
            "folder": f"{DRIVE_FOLDER_NAME}/{SHARED_FILE_NAME}"
        }
    except Exception as e:
        logger.error(f"Drive read error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/captions")
async def get_captions():
    """Get saved captions from Google Drive"""
    global drive_manager
    
    if not drive_manager:
        raise HTTPException(status_code=503, detail="Drive manager not initialized")
    
    try:
        data = await drive_manager.load_captions_data()
        
        if not data:
            return {"success": False, "message": "No captions data found"}
        
        return {
            "success": True,
            "data": data,
            "source": "google_drive",
            "folder": f"{DRIVE_FOLDER_NAME}/{CAPTIONS_FILE_NAME}"
        }
    except Exception as e:
        logger.error(f"Captions read error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/drive-topics")
async def get_drive_topics():
    """Get all topics with their reels from Google Drive"""
    global drive_manager
    
    if not drive_manager:
        raise HTTPException(status_code=503, detail="Drive manager not initialized")
    
    try:
        data = await drive_manager.load_shared_data()
        
        if not data:
            return {"success": False, "message": "No shared data found"}
        
        return {
            "success": True,
            "topics": data.get("topics", {}),
            "source": "google_drive",
            "folder": f"{DRIVE_FOLDER_NAME}/{SHARED_FILE_NAME}"
        }
    except Exception as e:
        logger.error(f"Drive read error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/drive-urls/{topic}")
async def get_drive_urls_by_topic(topic: str):
    """Get URLs for a specific topic from Google Drive"""
    global drive_manager
    
    if not drive_manager:
        raise HTTPException(status_code=503, detail="Drive manager not initialized")
    
    try:
        data = await drive_manager.load_shared_data()
        
        if not data:
            return {"success": False, "message": "No shared data found"}
        
        topics = data.get("topics", {})
        if topic not in topics:
            return {"success": True, "topic": topic, "count": 0, "urls": []}
        
        urls = []
        for reel in topics[topic]:
            if isinstance(reel, dict) and reel.get("url"):
                urls.append(reel["url"])
        
        return {
            "success": True,
            "topic": topic,
            "count": len(urls),
            "urls": urls,
            "source": "google_drive",
            "folder": f"{DRIVE_FOLDER_NAME}/{SHARED_FILE_NAME}"
        }
    except Exception as e:
        logger.error(f"Drive read error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "Instagram Reel Caption Capture",
        "version": "3.0.0",
        "timestamp": datetime.now().isoformat(),
        "drive_connected": drive_manager is not None and drive_manager.service is not None
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)