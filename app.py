"""
Instagram Reel Caption Capture - Web API for Render
Extracts clean captions from Instagram Reel URLs via API
Saves captions with URL mapping to Google Drive
AUTO-MODE: Automatically loads URLs from Drive and captures captions
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
import threading
import time
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict, field
from datetime import datetime
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
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

# Auto-mode configuration
AUTO_INTERVAL = int(os.environ.get("AUTO_INTERVAL", 30))  # Check every 60 seconds
AUTO_ENABLED = os.environ.get("AUTO_ENABLED", "true").lower() == "true"

# Global state for UI updates
auto_status = {
    "enabled": AUTO_ENABLED,
    "running": False,
    "last_run": None,
    "next_run": None,
    "total_captured": 0,
    "current_status": "Idle",
    "urls_processed": 0,
    "captions_captured": 0,
    "errors": 0,
    "log": []
}

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
    topic: str = ""
    
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
    
    def to_youtube_format(self) -> Dict[str, str]:
        """Convert caption data to YouTube-friendly format - NO METADATA FOOTER"""
        title = self._generate_title()
        description = self._generate_description()
        tags = [h for h in self.hashtags if h]
        
        return {
            "title": title,
            "description": description,
            "tags": tags,
            "shortcode": self.shortcode,
            "username": self.username,
            "topic": self.topic,
            "url": self.url
        }
    
    def _generate_title(self) -> str:
        title = self.caption.strip()
        title = re.sub(r'#\w+', '', title)
        title = re.sub(r'@\w+', '', title)
        title = ' '.join(title.split())
        
        if len(title) < 5:
            title = f"Instagram Reel - {self.shortcode}"
        if len(title) > 100:
            title = title[:97] + "..."
        if title:
            title = title[0].upper() + title[1:] if len(title) > 1 else title
        
        if self.topic and self.topic not in title:
            topic_lower = self.topic.lower()
            title_lower = title.lower()
            if topic_lower not in title_lower and topic_lower.replace('_', ' ') not in title_lower:
                title = f"{title} - #{self.topic}"
        
        return title
    
    def _generate_description(self) -> str:
        """Generate clean description - NO METADATA FOOTER"""
        description_parts = []
        
        raw_caption = self.full_caption if self.full_caption else self.caption
        clean_text = raw_caption
        
        # Remove metadata prefix/suffix
        clean_text = re.sub(r'^[\d,.]+K?\s*(?:likes?|❤️)\s*,\s*[\d,.]+K?\s*(?:comments?|💬)\s*-\s*[A-Za-z0-9_.]+\s+on\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}:\s*', '', clean_text, flags=re.IGNORECASE)
        clean_text = re.sub(r'\s*[\d,.]+K?\s*(?:likes?|❤️)\s*,\s*[\d,.]+K?\s*(?:comments?|💬)\s*-\s*[A-Za-z0-9_.]+\s+on\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}$', '', clean_text, flags=re.IGNORECASE)
        
        clean_text = re.sub(r'^"|"$', '', clean_text)
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        
        if clean_text and len(clean_text) > 3:
            description_parts.append(clean_text)
        
        if self.hashtags:
            description_parts.append("")
            description_parts.append(' '.join(['#' + h for h in self.hashtags if h]))
        
        description = "\n".join(description_parts)
        description = re.sub(r'\n{3,}', '\n\n', description)
        
        return description.strip()


class GoogleDriveManager:
    """Manage Google Drive operations for captions data"""
    
    def __init__(self):
        self.service = self._authenticate()
        self.folder_id = self._get_or_create_folder()
    
    def _authenticate(self):
        """Authenticate with Google Drive - supports Render environment"""
        creds = None
        token_file = 'drive_token.pickle'
        
        # 1. TRY ENVIRONMENT TOKEN (RENDER)
        token_json = os.environ.get('GOOGLE_DRIVE_TOKEN')
        if token_json:
            logger.info("🔑 GOOGLE_DRIVE_TOKEN found in environment")
            try:
                try:
                    decoded_bytes = base64.b64decode(token_json)
                    token_str = decoded_bytes.decode('utf-8')
                except:
                    token_str = token_json
                
                try:
                    token_data = json.loads(token_str)
                    if 'client_email' in token_data and 'private_key' in token_data:
                        from google.oauth2 import service_account
                        creds = service_account.Credentials.from_service_account_info(
                            token_data, scopes=SCOPES
                        )
                        logger.info("✅ Drive authenticated via service account")
                        return build('drive', 'v3', credentials=creds)
                    elif 'token' in token_data or 'refresh_token' in token_data:
                        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
                        logger.info("✅ Drive authenticated via OAuth2 token")
                        return build('drive', 'v3', credentials=creds)
                    else:
                        try:
                            creds = pickle.loads(base64.b64decode(token_json))
                            logger.info("✅ Drive authenticated via pickle (base64)")
                            return build('drive', 'v3', credentials=creds)
                        except:
                            pass
                except json.JSONDecodeError:
                    try:
                        creds = pickle.loads(base64.b64decode(token_json))
                        logger.info("✅ Drive authenticated via pickle")
                        return build('drive', 'v3', credentials=creds)
                    except:
                        pass
            except Exception as e:
                logger.warning(f"GOOGLE_DRIVE_TOKEN failed: {e}")
        
        # 2. TRY LOCAL TOKEN FILE
        if os.path.exists(token_file):
            try:
                with open(token_file, 'rb') as f:
                    creds = pickle.load(f)
                if creds and hasattr(creds, 'expired') and creds.expired and hasattr(creds, 'refresh_token'):
                    creds.refresh(Request())
                    logger.info("🔄 Refreshed Drive token")
                    with open(token_file, 'wb') as f:
                        pickle.dump(creds, f)
                logger.info("✅ Drive authenticated via drive_token.pickle")
                return build('drive', 'v3', credentials=creds)
            except Exception as e:
                logger.warning(f"drive_token.pickle failed: {e}")
        
        # 3. TRY CREDENTIALS.JSON (LOCAL DEV ONLY)
        if os.path.exists('credentials.json'):
            try:
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
                logger.info("✅ Drive authenticated via credentials.json")
                with open(token_file, 'wb') as f:
                    pickle.dump(creds, f)
                return build('drive', 'v3', credentials=creds)
            except Exception as e:
                logger.warning(f"credentials.json auth failed: {e}")
        
        # 4. TRY SERVICE ACCOUNT FROM ENV
        credentials_json = os.environ.get('GOOGLE_CREDENTIALS')
        if credentials_json:
            try:
                try:
                    credentials_data = json.loads(base64.b64decode(credentials_json).decode('utf-8'))
                except:
                    credentials_data = json.loads(credentials_json)
                
                if 'client_email' in credentials_data and 'private_key' in credentials_data:
                    from google.oauth2 import service_account
                    creds = service_account.Credentials.from_service_account_info(
                        credentials_data, scopes=SCOPES
                    )
                    logger.info("✅ Drive authenticated via GOOGLE_CREDENTIALS")
                    return build('drive', 'v3', credentials=creds)
            except Exception as e:
                logger.warning(f"GOOGLE_CREDENTIALS failed: {e}")
        
        logger.error("❌ No Drive credentials found!")
        return None
    
    def _get_or_create_folder(self):
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
        if not self.service or not self.folder_id:
            logger.error("No Drive service available")
            return False
        
        try:
            # Merge with existing captions to avoid duplicates
            existing = await self.load_captions_data()
            existing_captions = existing.get('captions', []) if existing else []
            
            # Build set of existing URLs
            existing_urls = {c.get('url') for c in existing_captions}
            
            # Add new captions that don't already exist
            all_captions = existing_captions.copy()
            new_count = 0
            for c in captions:
                if c.url not in existing_urls:
                    all_captions.append(c.to_dict())
                    new_count += 1
            
            if new_count == 0:
                logger.info("No new captions to add")
                return True
            
            # Prepare data with YouTube-friendly format
            data = {
                "timestamp": datetime.now().isoformat(),
                "total_captures": len(all_captions),
                "captions": all_captions,
                "youtube_ready": [ReelCaptionData(**c).to_youtube_format() for c in all_captions],
                "url_mapping": {c.get('url'): c.get('shortcode') for c in all_captions},
                "shortcode_mapping": {c.get('shortcode'): c.get('url') for c in all_captions},
                "source": "caption_capture_auto",
                "last_updated": datetime.now().isoformat()
            }
            
            query = f"'{self.folder_id}' in parents and name='{CAPTIONS_FILE_NAME}' and trashed=false"
            results = self.service.files().list(q=query, fields="files(id)").execute()
            files = results.get('files', [])
            
            file_content = json.dumps(data, indent=2, ensure_ascii=False)
            from googleapiclient.http import MediaIoBaseUpload
            file_stream = io.BytesIO(file_content.encode('utf-8'))
            media = MediaIoBaseUpload(file_stream, mimetype='application/json', resumable=True)
            
            if files:
                file_id = files[0]['id']
                logger.info(f"📤 Updating: {CAPTIONS_FILE_NAME} (+{new_count} new)")
                self.service.files().update(fileId=file_id, media_body=media).execute()
                logger.info(f"✅ Updated captions file: {CAPTIONS_FILE_NAME}")
            else:
                logger.info(f"📤 Creating: {CAPTIONS_FILE_NAME}")
                file_metadata = {
                    'name': CAPTIONS_FILE_NAME,
                    'parents': [self.folder_id],
                    'description': f"Instagram Reel Captions - {datetime.now().strftime('%Y-%m-%d')}"
                }
                self.service.files().create(body=file_metadata, media_body=media).execute()
                logger.info(f"✅ Created captions file: {CAPTIONS_FILE_NAME}")
            
            file_stream.close()
            return True
        except Exception as e:
            logger.error(f"Save to Drive error: {e}")
            return False
    
    async def load_captions_data(self) -> Optional[Dict]:
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
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("🔚 Browser closed")
    
    def extract_shortcode(self, url: str) -> Optional[str]:
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
                        'header h2', 'header a[href*="/"]', 'article header a',
                        'div._a9zr a', 'a._a9zr', 'span._a9zr a',
                        'div[class*="username"] a', 'h2[class*="username"]'
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
                        'div._a9zr', 'div.C4VMK span', 'div.weLOq span',
                        'h1._a9zr', 'div._a9zs', 'article div._a9zr',
                        'div[class*="caption"]', 'div[class*="Caption"]'
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
        results = []
        topics = topics or {}
        
        for url in urls:
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
auto_thread = None
loop = None


# ============================================================
# AUTO CAPTURE BACKGROUND THREAD
# ============================================================
def run_auto_capture():
    """Background thread that automatically captures captions from Drive URLs"""
    global auto_status, extractor, drive_manager, loop
    
    logger.info(f"🔄 Auto-capture thread started (interval: {AUTO_INTERVAL}s)")
    auto_status["running"] = True
    
    while auto_status["enabled"]:
        try:
            auto_status["current_status"] = "Loading URLs from Drive..."
            auto_status["next_run"] = (datetime.now().timestamp() + AUTO_INTERVAL)
            
            # Run async code in the main event loop
            if loop is not None:
                # Load URLs from Drive
                shared_data = asyncio.run_coroutine_threadsafe(
                    drive_manager.load_shared_data(), loop
                ).result(timeout=60)
                
                if shared_data:
                    urls = []
                    topics = {}
                    
                    # Extract URLs from topics
                    for topic, reels in shared_data.get("topics", {}).items():
                        for reel in reels:
                            if isinstance(reel, dict) and reel.get("url"):
                                url = reel["url"]
                                urls.append(url)
                                topics[url] = topic
                    
                    auto_status["urls_processed"] = len(urls)
                    
                    if urls:
                        auto_status["current_status"] = f"Capturing {len(urls)} URLs..."
                        
                        # Get existing captions to avoid duplicates
                        existing_data = asyncio.run_coroutine_threadsafe(
                            drive_manager.load_captions_data(), loop
                        ).result(timeout=60)
                        
                        existing_urls = set()
                        if existing_data:
                            for c in existing_data.get("captions", []):
                                existing_urls.add(c.get("url"))
                        
                        # Filter out already captured URLs
                        new_urls = [u for u in urls if u not in existing_urls]
                        
                        if new_urls:
                            auto_status["current_status"] = f"Capturing {len(new_urls)} new URLs..."
                            
                            # Capture captions
                            results = asyncio.run_coroutine_threadsafe(
                                extractor.get_multiple_captions(new_urls, topics), loop
                            ).result(timeout=300)  # 5 minute timeout
                            
                            if results:
                                # Save to Drive
                                save_success = asyncio.run_coroutine_threadsafe(
                                    drive_manager.save_captions_data(results), loop
                                ).result(timeout=60)
                                
                                if save_success:
                                    auto_status["captions_captured"] += len(results)
                                    auto_status["total_captured"] += len(results)
                                    auto_status["current_status"] = f"✅ Captured {len(results)} new captions"
                                    
                                    # Add to log
                                    log_entry = {
                                        "timestamp": datetime.now().isoformat(),
                                        "urls": len(new_urls),
                                        "captured": len(results),
                                        "status": "success"
                                    }
                                    auto_status["log"].append(log_entry)
                                    if len(auto_status["log"]) > 100:
                                        auto_status["log"] = auto_status["log"][-100:]
                                    
                                    logger.info(f"✅ Auto-captured {len(results)} new captions")
                                else:
                                    auto_status["errors"] += 1
                                    auto_status["current_status"] = "❌ Failed to save to Drive"
                            else:
                                auto_status["current_status"] = "⚠️ No new captions captured"
                        else:
                            auto_status["current_status"] = "✅ All URLs already captured"
                    else:
                        auto_status["current_status"] = "⚠️ No URLs found in Drive"
                else:
                    auto_status["current_status"] = "⚠️ Could not load shared data"
            
            auto_status["last_run"] = datetime.now().isoformat()
            
        except Exception as e:
            auto_status["errors"] += 1
            auto_status["current_status"] = f"❌ Error: {str(e)[:50]}"
            logger.error(f"Auto-capture error: {e}")
        
        # Sleep until next interval
        time.sleep(AUTO_INTERVAL)


@app.on_event("startup")
async def startup_event():
    global extractor, drive_manager, auto_thread, loop
    
    logger.info("🚀 Starting up...")
    loop = asyncio.get_event_loop()
    
    # Initialize Google Drive Manager
    drive_manager = GoogleDriveManager()
    if drive_manager.service:
        logger.info("📁 Google Drive Manager initialized successfully")
    else:
        logger.error("❌ Google Drive Manager initialization failed")
    
    # Initialize Caption Extractor
    extractor = InstagramCaptionExtractor(headless=True)
    await extractor.initialize()
    logger.info("🚀 Caption extractor initialized")
    
    # Start auto-capture thread if enabled
    if AUTO_ENABLED:
        auto_status["enabled"] = True
        auto_thread = threading.Thread(target=run_auto_capture, daemon=True)
        auto_thread.start()
        logger.info(f"🔄 Auto-capture started (interval: {AUTO_INTERVAL}s)")


@app.on_event("shutdown")
async def shutdown_event():
    global extractor, auto_status
    auto_status["enabled"] = False
    auto_status["running"] = False
    if extractor:
        await extractor.close()
        logger.info("🔚 Browser closed")


# ============================================================
# ROUTES
# ============================================================
@app.get("/")
async def root():
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Instagram Reel Caption Capture - Auto Mode</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { background: #0a0a0f; color: #fff; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 20px; }
            .container { background: #1a1a1a; padding: 40px; border-radius: 16px; max-width: 1000px; width: 100%; }
            h1 { color: #dc2743; font-size: 32px; margin-bottom: 10px; display: flex; align-items: center; gap: 10px; }
            .subtitle { color: #888; font-size: 16px; margin-bottom: 30px; }
            .auto-status {
                background: #2a2a2a;
                padding: 15px 20px;
                border-radius: 10px;
                margin-bottom: 20px;
                border-left: 3px solid #4ade80;
            }
            .auto-status .status-dot {
                display: inline-block;
                width: 12px;
                height: 12px;
                border-radius: 50%;
                margin-right: 10px;
                animation: pulse 2s infinite;
            }
            .status-dot.running { background: #4ade80; }
            .status-dot.stopped { background: #ff4444; }
            @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
            .auto-stats {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
                gap: 10px;
                margin: 15px 0;
            }
            .auto-stat {
                background: #0a0a0f;
                padding: 10px 15px;
                border-radius: 8px;
                text-align: center;
            }
            .auto-stat .value { font-size: 24px; font-weight: bold; color: #4ade80; }
            .auto-stat .label { font-size: 11px; color: #888; margin-top: 2px; }
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
            #results { margin-top: 20px; max-height: 500px; overflow-y: auto; }
            .result-item { background: #2a2a2a; padding: 15px; border-radius: 10px; margin-bottom: 10px; border-left: 3px solid #dc2743; }
            .result-item .caption { color: #ddd; margin: 5px 0; }
            .result-item .meta { color: #888; font-size: 13px; }
            .result-item .hashtags { color: #4ade80; font-size: 13px; }
            .loading { text-align: center; padding: 20px; color: #888; }
            .error { color: #ff4444; }
            .log-box {
                background: #0a0a0a;
                padding: 10px;
                border-radius: 8px;
                margin: 10px 0;
                max-height: 150px;
                overflow-y: auto;
                font-family: monospace;
                font-size: 12px;
                color: #888;
            }
            .log-box .log-entry { padding: 2px 0; border-bottom: 1px solid #1a1a1a; }
            .log-box .success { color: #4ade80; }
            .log-box .error { color: #ff4444; }
            .footer { margin-top: 20px; color: #666; font-size: 12px; text-align: center; border-top: 1px solid #2a2a2a; padding-top: 20px; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎬 Reel Caption Capture <span style="font-size: 16px; color: #4ade80;">🤖 AUTO</span></h1>
            <p class="subtitle">Automatically captures captions from Drive URLs every {{ interval }}s</p>
            
            <div class="auto-status" id="autoStatus">
                <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap;">
                    <div>
                        <span class="status-dot running" id="statusDot"></span>
                        <span id="statusText">Starting...</span>
                    </div>
                    <div style="display: flex; gap: 10px;">
                        <button class="btn btn-secondary" onclick="toggleAuto()" id="toggleBtn" style="padding: 8px 16px; font-size: 12px;">⏸️ Pause</button>
                        <button class="btn btn-secondary" onclick="runNow()" id="runNowBtn" style="padding: 8px 16px; font-size: 12px;">▶️ Run Now</button>
                    </div>
                </div>
                <div class="auto-stats" id="stats">
                    <div class="auto-stat"><div class="value" id="totalCaptured">0</div><div class="label">Total Captured</div></div>
                    <div class="auto-stat"><div class="value" id="urlsProcessed">0</div><div class="label">URLs Processed</div></div>
                    <div class="auto-stat"><div class="value" id="errorsCount">0</div><div class="label">Errors</div></div>
                    <div class="auto-stat"><div class="value" id="lastRun">-</div><div class="label">Last Run</div></div>
                </div>
                <div style="font-size: 12px; color: #666; margin-top: 10px;">
                    Next run: <span id="nextRun">-</span> | Status: <span id="currentStatus">Idle</span>
                </div>
            </div>
            
            <div class="drive-status">
                📁 Saving to: <strong>Google Drive (Reel_Finder_Data/captions_data.json)</strong>
                <br>📋 URLs from: <strong>Reel_Finder_Data/shared_reels.json</strong>
                <br>🔄 Auto-capture: <strong id="autoEnabled">Enabled</strong>
            </div>
            
            <div class="input-section">
                <h3 style="margin-bottom: 10px; color: #ccc; font-size: 14px;">📝 Manual Entry (optional)</h3>
                <textarea id="urls" placeholder="https://www.instagram.com/reel/DZvVnIdxMla/&#10;https://www.instagram.com/reel/DZszmLkhAdY/"></textarea>
                
                <div class="btn-group">
                    <button class="btn" onclick="capture()" id="captureBtn">🔍 Capture & Save</button>
                    <button class="btn btn-secondary" onclick="loadFromDrive()">📥 Load URLs from Drive</button>
                    <button class="btn btn-secondary" onclick="clearResults()">🗑️ Clear</button>
                    <button class="btn btn-secondary" onclick="refreshStatus()">🔄 Refresh Status</button>
                </div>
            </div>
            
            <div id="results"></div>
            
            <div class="log-box" id="logBox">
                <div style="color: #666; margin-bottom: 5px;">📋 Activity Log</div>
                <div id="logEntries">Loading...</div>
            </div>
            
            <div class="footer">
                Powered by Playwright • Google Drive • FastAPI • 🤖 Auto-Mode
            </div>
        </div>
        
        <script>
            let autoEnabled = true;
            
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
                            <div style="display: flex; gap: 20px; margin: 10px 0; flex-wrap: wrap;">
                                <span style="background: #2a2a2a; padding: 8px 15px; border-radius: 8px; font-size: 13px;">📊 Total: <strong>${data.count}</strong></span>
                                <span style="background: #2a2a2a; padding: 8px 15px; border-radius: 8px; font-size: 13px;">📥 Captured: <strong>${data.captured}</strong></span>
                                <span style="background: #2a2a2a; padding: 8px 15px; border-radius: 8px; font-size: 13px;">💾 Saved to Drive: <strong>✅</strong></span>
                            </div>
                        `;
                        
                        data.reels.forEach((reel, i) => {
                            const hashtags = reel.hashtags ? reel.hashtags.map(h => '#' + h).join(' ') : '';
                            const youtube_title = data.youtube_ready && data.youtube_ready[i] ? data.youtube_ready[i].title : reel.caption;
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
                        refreshStatus();
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
                        resultsDiv.innerHTML = `<div style="background: #10b98120; color: #4ade80; padding: 15px; border-radius: 10px;">
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
            
            async function refreshStatus() {
                try {
                    const response = await fetch('/auto-status');
                    const data = await response.json();
                    
                    document.getElementById('totalCaptured').textContent = data.total_captured || 0;
                    document.getElementById('urlsProcessed').textContent = data.urls_processed || 0;
                    document.getElementById('errorsCount').textContent = data.errors || 0;
                    document.getElementById('lastRun').textContent = data.last_run ? new Date(data.last_run).toLocaleTimeString() : '-';
                    document.getElementById('nextRun').textContent = data.next_run ? new Date(data.next_run * 1000).toLocaleTimeString() : '-';
                    document.getElementById('currentStatus').textContent = data.current_status || 'Idle';
                    document.getElementById('autoEnabled').textContent = data.enabled ? '✅ Enabled' : '⏸️ Paused';
                    
                    const dot = document.getElementById('statusDot');
                    const statusText = document.getElementById('statusText');
                    if (data.running) {
                        dot.className = 'status-dot running';
                        statusText.textContent = '🔄 Running';
                    } else {
                        dot.className = 'status-dot stopped';
                        statusText.textContent = data.enabled ? '⏸️ Paused' : '⏹️ Stopped';
                    }
                    
                    // Update log
                    if (data.log && data.log.length > 0) {
                        let logHtml = '';
                        data.log.slice(-10).reverse().forEach(entry => {
                            const time = new Date(entry.timestamp).toLocaleTimeString();
                            const statusClass = entry.status === 'success' ? 'success' : 'error';
                            logHtml += `<div class="log-entry ${statusClass}">[${time}] ${entry.status === 'success' ? '✅' : '❌'} ${entry.urls || 0} URLs → ${entry.captured || 0} captured</div>`;
                        });
                        document.getElementById('logEntries').innerHTML = logHtml || 'No entries yet';
                    }
                } catch (error) {
                    console.error('Refresh error:', error);
                }
            }
            
            async function toggleAuto() {
                const btn = document.getElementById('toggleBtn');
                const current = btn.textContent.includes('Pause');
                const action = current ? 'pause' : 'resume';
                
                try {
                    const response = await fetch(`/auto-${action}`, { method: 'POST' });
                    const data = await response.json();
                    
                    if (data.success) {
                        autoEnabled = !autoEnabled;
                        btn.textContent = autoEnabled ? '⏸️ Pause' : '▶️ Resume';
                        refreshStatus();
                    }
                } catch (error) {
                    console.error('Toggle error:', error);
                }
            }
            
            async function runNow() {
                const btn = document.getElementById('runNowBtn');
                btn.disabled = true;
                btn.textContent = '⏳ Running...';
                
                try {
                    const response = await fetch('/auto-run-now', { method: 'POST' });
                    const data = await response.json();
                    if (data.success) {
                        refreshStatus();
                        setTimeout(refreshStatus, 3000);
                    }
                } catch (error) {
                    console.error('Run now error:', error);
                } finally {
                    btn.disabled = false;
                    btn.textContent = '▶️ Run Now';
                }
            }
            
            function clearResults() {
                document.getElementById('results').innerHTML = '';
            }
            
            // Refresh status every 5 seconds
            refreshStatus();
            setInterval(refreshStatus, 5000);
        </script>
    </body>
    </html>
    """)

@app.get("/auto-status")
async def get_auto_status():
    """Get current auto-capture status"""
    global auto_status
    return {
        "enabled": auto_status["enabled"],
        "running": auto_status["running"],
        "last_run": auto_status["last_run"],
        "next_run": auto_status["next_run"],
        "total_captured": auto_status["total_captured"],
        "current_status": auto_status["current_status"],
        "urls_processed": auto_status["urls_processed"],
        "captions_captured": auto_status["captions_captured"],
        "errors": auto_status["errors"],
        "log": auto_status["log"][-20:]  # Last 20 log entries
    }

@app.post("/auto-pause")
async def pause_auto():
    """Pause auto-capture"""
    global auto_status
    auto_status["enabled"] = False
    auto_status["current_status"] = "⏸️ Paused by user"
    return {"success": True, "message": "Auto-capture paused"}

@app.post("/auto-resume")
async def resume_auto():
    """Resume auto-capture"""
    global auto_status, auto_thread
    auto_status["enabled"] = True
    auto_status["current_status"] = "Resuming..."
    
    # If thread is not running, start it
    if not auto_status["running"]:
        auto_thread = threading.Thread(target=run_auto_capture, daemon=True)
        auto_thread.start()
    
    return {"success": True, "message": "Auto-capture resumed"}

@app.post("/auto-run-now")
async def run_now():
    """Manually trigger auto-capture immediately"""
    global auto_status
    
    if auto_status["running"]:
        # Trigger a run by resetting the thread
        auto_status["enabled"] = True
        # The background thread will run on its next cycle
        return {"success": True, "message": "Auto-capture will run on next cycle"}
    else:
        # Start the thread
        auto_status["enabled"] = True
        thread = threading.Thread(target=run_auto_capture, daemon=True)
        thread.start()
        return {"success": True, "message": "Auto-capture started"}

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
        shared_data = await drive_manager.load_shared_data()
        topics_map = {}
        if shared_data and shared_data.get("topics"):
            for topic, reels in shared_data["topics"].items():
                for reel in reels:
                    if reel.get("url"):
                        topics_map[reel["url"]] = topic
        
        results = await extractor.get_multiple_captions(urls, topics_map)
        youtube_ready = [r.to_youtube_format() for r in results]
        save_success = await drive_manager.save_captions_data(results)
        
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
        
        # Update auto status
        auto_status["captions_captured"] += len(serializable_results)
        auto_status["total_captured"] += len(serializable_results)
        
        return {
            "success": True,
            "count": len(serializable_results),
            "captured": len(serializable_results),
            "saved_to_drive": save_success,
            "reels": serializable_results,
            "youtube_ready": youtube_ready,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Capture error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/capture")
async def capture_captions_get(urls: str = Query(..., description="Comma-separated reel URLs")):
    url_list = [u.strip() for u in urls.split(',') if u.strip()]
    return await capture_captions({"urls": url_list})

@app.get("/drive-urls")
async def get_drive_urls():
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

@app.get("/youtube-ready")
async def get_youtube_ready_captions():
    global drive_manager
    
    if not drive_manager:
        raise HTTPException(status_code=503, detail="Drive manager not initialized")
    
    try:
        data = await drive_manager.load_captions_data()
        
        if not data:
            return {"success": False, "message": "No captions data found"}
        
        return {
            "success": True,
            "youtube_ready": data.get("youtube_ready", []),
            "total": len(data.get("youtube_ready", [])),
            "source": "google_drive",
            "folder": f"{DRIVE_FOLDER_NAME}/{CAPTIONS_FILE_NAME}"
        }
    except Exception as e:
        logger.error(f"Youtube ready error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/caption/{shortcode}")
async def get_caption_by_shortcode(shortcode: str):
    global drive_manager
    
    if not drive_manager:
        raise HTTPException(status_code=503, detail="Drive manager not initialized")
    
    try:
        caption = await drive_manager.get_caption_by_shortcode(shortcode)
        
        if not caption:
            return {"success": False, "message": f"No caption found for shortcode: {shortcode}"}
        
        youtube_ready = await drive_manager.get_youtube_ready_by_shortcode(shortcode)
        
        return {
            "success": True,
            "caption": caption,
            "youtube_ready": youtube_ready,
            "shortcode": shortcode
        }
    except Exception as e:
        logger.error(f"Caption fetch error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/drive-topics")
async def get_drive_topics():
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


# ============================================================
# MAIN ENTRY POINT
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    
    print(f"\n{'='*50}")
    print(f"🚀 Instagram Reel Caption Capture v3.0 - AUTO MODE")
    print(f"{'='*50}")
    print(f"🌐 Server: http://0.0.0.0:{port}")
    print(f"📁 Drive folder: {DRIVE_FOLDER_NAME}")
    print(f"📄 Shared file: {SHARED_FILE_NAME}")
    print(f"📄 Captions file: {CAPTIONS_FILE_NAME}")
    print(f"🎬 YouTube-ready format: Included")
    print(f"🔄 Auto-capture: {'ENABLED' if AUTO_ENABLED else 'DISABLED'} (interval: {AUTO_INTERVAL}s)")
    print(f"{'='*50}\n")
    
    uvicorn.run(app, host="0.0.0.0", port=port)