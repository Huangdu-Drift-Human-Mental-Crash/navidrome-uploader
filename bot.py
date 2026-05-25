"""
Telegram bot: receive audio files, process metadata, save to Navidrome music folder, trigger scan.
"""
import asyncio
import base64
import os
import re
import shlex
import logging
import secrets
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests as http_requests
from dotenv import load_dotenv
from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TCON, APIC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
import shutil
import tempfile
from telegram import BotCommand, BotCommandScopeAllPrivateChats, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, ContextTypes, filters

from lyric_filler import search_all_sources, preview_lrc, write_lyrics, has_synced_lyrics

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
ALLOWED_USERS = set(int(x) for x in os.getenv('ALLOWED_USERS', '').split(',') if x.strip())
PROXY_URL = os.getenv('PROXY_URL', '').strip() or None
NAVIDROME_URL = os.getenv('NAVIDROME_URL', 'http://localhost:4533')
NAVIDROME_USER = os.getenv('NAVIDROME_USER', 'admin')
NAVIDROME_PASS = os.getenv('NAVIDROME_PASS', '')
MUSIC_FOLDER = Path(os.getenv('MUSIC_FOLDER', './music'))
POST_HOOK = os.getenv('POST_HOOK', '').strip()
API_ID = os.getenv('API_ID', '').strip()
API_HASH = os.getenv('API_HASH', '').strip()

# Pyrogram client for large file downloads (>20MB)
pyro_client = None
if API_ID and API_HASH:
    from pyrogram import Client as PyroClient
    proxy_cfg = None
    if PROXY_URL:
        p = urlparse(PROXY_URL)
        proxy_cfg = {'scheme': p.scheme, 'hostname': p.hostname, 'port': p.port}
    pyro_client = PyroClient(
        "navidrome_bot",
        api_id=int(API_ID),
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        proxy=proxy_cfg,
        no_updates=True,
    )

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
# Suppress verbose httpx/telegram HTTP request logs
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

EDIT_FIELD_LABELS = {
    'artist': 'Artist',
    'title': 'Title',
    'album': 'Album',
    'genre': 'Genre',
}
MAX_RECENT_UPLOADS = 50
MAX_EDIT_UPLOAD_CHOICES = 10
MAX_LIBRARY_SEARCH_RESULTS = 10
MAX_LYRICS_SETS_PER_UPLOAD = 5
POST_HOOK_TIMEOUT = 60
COVER_IMAGE_MAX_BYTES = 15 * 1024 * 1024
COVER_PAGE_MAX_BYTES = 2 * 1024 * 1024
SUPPORTED_AUDIO_EXTS = ('.mp3', '.flac', '.m4a', '.ogg', '.wav', '.aac', '.wma', '.opus')
COVER_FETCH_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; NavidromeUploaderBot/1.0)',
    'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
}


def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


def build_post_hook_args(filepath: str) -> list[str]:
    """Build a safe argv list for the configured post-processing hook."""
    args = shlex.split(POST_HOOK)
    return [arg.replace('{path}', filepath) for arg in args]


def format_hook_output(output: bytes) -> str:
    text = output.decode(errors='replace').strip()
    if len(text) > 4000:
        return f"{text[:4000]}... [truncated]"
    return text


async def run_post_hook(filepath: str):
    """Run user-defined post-processing command without invoking a shell."""
    if not POST_HOOK:
        return

    process = None
    try:
        args = build_post_hook_args(filepath)
        if not args:
            return

        env = os.environ.copy()
        env['NAVIDROME_UPLOADER_PATH'] = filepath
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=POST_HOOK_TIMEOUT)
        if process.returncode:
            logger.warning(
                "Post-hook exited with code %s for %s. stdout=%r stderr=%r",
                process.returncode,
                filepath,
                format_hook_output(stdout),
                format_hook_output(stderr),
            )
    except asyncio.TimeoutError:
        if process:
            process.kill()
            await process.communicate()
        logger.warning("Post-hook timed out after %s seconds for %s", POST_HOOK_TIMEOUT, filepath)
    except Exception as e:
        logger.warning(f"Post-hook failed: {e}")


def schedule_post_hook(filepath: str):
    """Schedule post-processing without delaying the Telegram handler."""
    if POST_HOOK:
        asyncio.create_task(run_post_hook(filepath))


def trigger_scan():
    """Trigger Navidrome library scan via Subsonic API."""
    try:
        resp = http_requests.get(
            f"{NAVIDROME_URL}/rest/startScan",
            params={'u': NAVIDROME_USER, 'p': NAVIDROME_PASS, 'v': '1.16.1', 'c': 'tgbot', 'f': 'json'},
            timeout=10
        )
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Scan trigger failed: {e}")
        return False


def get_metadata(filepath: Path) -> dict:
    """Extract metadata from audio file."""
    audio = MutagenFile(str(filepath))
    if audio is None:
        return {}
    meta = {'artist': 'Unknown', 'title': filepath.stem, 'album': '', 'genre': ''}
    if getattr(audio, 'info', None) and getattr(audio.info, 'length', None):
        meta['duration'] = int(audio.info.length * 1000)

    tags = audio.tags
    if tags is None:
        return meta

    # ID3
    if 'TPE1' in tags:
        meta['artist'] = str(tags['TPE1'])
    if 'TIT2' in tags:
        meta['title'] = str(tags['TIT2'])
    if 'TALB' in tags:
        meta['album'] = str(tags['TALB'])
    if 'TCON' in tags:
        meta['genre'] = str(tags['TCON'])

    # Vorbis/FLAC
    if hasattr(tags, 'get'):
        if meta['artist'] == 'Unknown':
            meta['artist'] = (tags.get('artist') or tags.get('ARTIST') or ['Unknown'])[0]
        if meta['title'] == filepath.stem:
            meta['title'] = (tags.get('title') or tags.get('TITLE') or [filepath.stem])[0]
        if not meta['album']:
            meta['album'] = (tags.get('album') or tags.get('ALBUM') or [''])[0]
        if not meta['genre']:
            meta['genre'] = (tags.get('genre') or tags.get('GENRE') or [''])[0]

    # MP4
    if isinstance(audio, MP4):
        meta['artist'] = (audio.tags.get('\xa9ART') or ['Unknown'])[0]
        meta['title'] = (audio.tags.get('\xa9nam') or [filepath.stem])[0]
        meta['album'] = (audio.tags.get('\xa9alb') or [''])[0]
        meta['genre'] = (audio.tags.get('\xa9gen') or [''])[0]

    return meta


def sanitize_filename(name: str) -> str:
    """Remove invalid filename characters."""
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip('. ') or 'Unknown'


def determine_path(meta: dict, ext: str) -> Path:
    """Determine destination path: {MUSIC_FOLDER}/{Artist}/{Title}.ext"""
    artist = sanitize_filename(meta.get('artist', 'Unknown'))
    title = sanitize_filename(meta.get('title', 'Unknown'))
    dest_dir = MUSIC_FOLDER / artist
    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir / f"{title}{ext}"


def write_metadata(filepath: Path, meta: dict):
    """Write editable metadata fields back into the audio file."""
    audio = MutagenFile(str(filepath))
    if audio is None:
        raise ValueError("Unsupported or unreadable audio file.")

    artist = meta.get('artist', 'Unknown')
    title = meta.get('title', filepath.stem)
    album = meta.get('album', '')
    genre = meta.get('genre', '')
    ext = filepath.suffix.lower()

    if ext == '.mp3':
        tags = audio.tags or ID3()
        tags.delall('TPE1')
        tags.delall('TIT2')
        tags.delall('TALB')
        tags.delall('TCON')
        tags.add(TPE1(encoding=3, text=artist))
        tags.add(TIT2(encoding=3, text=title))
        if album:
            tags.add(TALB(encoding=3, text=album))
        if genre:
            tags.add(TCON(encoding=3, text=genre))
        tags.save(str(filepath))
        return

    if audio.tags is None:
        audio.add_tags()

    if isinstance(audio, MP4) or ext == '.m4a':
        audio.tags['\xa9ART'] = [artist]
        audio.tags['\xa9nam'] = [title]
        if album:
            audio.tags['\xa9alb'] = [album]
        else:
            audio.tags.pop('\xa9alb', None)
        if genre:
            audio.tags['\xa9gen'] = [genre]
        else:
            audio.tags.pop('\xa9gen', None)
        audio.save()
        return

    if hasattr(audio.tags, '__setitem__'):
        audio.tags['artist'] = [artist]
        audio.tags['title'] = [title]
        if album:
            audio.tags['album'] = [album]
        else:
            try:
                del audio.tags['album']
            except KeyError:
                pass
        if genre:
            audio.tags['genre'] = [genre]
        else:
            try:
                del audio.tags['genre']
            except KeyError:
                pass
        audio.save()
        return

    raise ValueError(f"Unsupported metadata format: {ext}")


def detect_image_mime(image_data: bytes) -> str | None:
    if image_data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if image_data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    return None


def make_cover_picture(image_data: bytes, mime: str) -> Picture:
    picture = Picture()
    picture.type = 3
    picture.mime = mime
    picture.desc = 'Cover'
    picture.data = image_data
    return picture


def remove_tag_if_present(tags, key: str):
    try:
        del tags[key]
    except KeyError:
        pass


def write_cover(filepath: Path, image_data: bytes, mime: str):
    """Write front cover art into the audio file."""
    audio = MutagenFile(str(filepath))
    if audio is None:
        raise ValueError("Unsupported or unreadable audio file.")

    ext = filepath.suffix.lower()
    if ext == '.mp3':
        tags = audio.tags or ID3()
        tags.delall('APIC')
        tags.add(APIC(encoding=3, mime=mime, type=3, desc='Cover', data=image_data))
        tags.save(str(filepath))
        return

    if isinstance(audio, MP4) or ext == '.m4a':
        if audio.tags is None:
            audio.add_tags()
        image_format = MP4Cover.FORMAT_JPEG if mime == 'image/jpeg' else MP4Cover.FORMAT_PNG
        audio.tags['covr'] = [MP4Cover(image_data, imageformat=image_format)]
        audio.save()
        return

    picture = make_cover_picture(image_data, mime)
    if isinstance(audio, FLAC):
        audio.clear_pictures()
        audio.add_picture(picture)
        audio.save()
        return

    if ext in ('.ogg', '.opus'):
        if audio.tags is None:
            audio.add_tags()
        for key in ('metadata_block_picture', 'METADATA_BLOCK_PICTURE', 'coverart', 'COVERART',
                    'coverartmime', 'COVERARTMIME'):
            remove_tag_if_present(audio.tags, key)
        audio.tags['metadata_block_picture'] = [base64.b64encode(picture.write()).decode('ascii')]
        audio.save()
        return

    raise ValueError(f"Cover editing is not supported for {ext or 'this file type'}.")


class CoverImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag: str, attrs: list):
        attrs = {key.lower(): value for key, value in attrs if key and value}
        if tag == 'meta':
            key = (attrs.get('property') or attrs.get('name') or attrs.get('itemprop') or '').lower()
            if key in ('og:image', 'og:image:url', 'twitter:image', 'twitter:image:src',
                       'thumbnail', 'thumbnailurl'):
                self.urls.append(attrs.get('content', ''))
        elif tag == 'link':
            rel = attrs.get('rel', '').lower()
            if 'image_src' in rel or 'thumbnail' in rel:
                self.urls.append(attrs.get('href', ''))


def extract_first_url(text: str) -> str | None:
    match = re.search(r'https?://\S+', text.strip())
    if not match:
        return None
    return match.group(0).rstrip('.,，。)')


def validate_http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError("Only http(s) URLs are supported.")
    return url


def cover_fetch_headers(url: str, accept: str | None = None) -> dict:
    headers = dict(COVER_FETCH_HEADERS)
    if accept:
        headers['Accept'] = accept
    host = urlparse(url).netloc.casefold()
    if 'bilibili.com' in host or 'hdslb.com' in host:
        headers['Referer'] = 'https://www.bilibili.com/'
    return headers


def parse_youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.casefold().split(':')[0]
    path_parts = [part for part in parsed.path.split('/') if part]

    if host.endswith('youtu.be') and path_parts:
        return path_parts[0]
    if 'youtube.com' not in host:
        return None

    query_id = parse_qs(parsed.query).get('v', [''])[0]
    if query_id:
        return query_id
    if len(path_parts) >= 2 and path_parts[0] in ('shorts', 'embed', 'v'):
        return path_parts[1]
    return None


def youtube_cover_candidates(url: str) -> list[str]:
    video_id = parse_youtube_video_id(url)
    if not video_id:
        return []
    return [
        f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
        f"https://img.youtube.com/vi/{video_id}/sddefault.jpg",
        f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
    ]


def parse_bilibili_video_id(url: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    host = parsed.netloc.casefold().split(':')[0]
    if 'bilibili.com' not in host and 'b23.tv' not in host:
        return None, None

    match = re.search(r'/video/(BV[0-9A-Za-z]+)', parsed.path)
    if match:
        return match.group(1), None

    match = re.search(r'/video/av(\d+)', parsed.path, re.IGNORECASE)
    if match:
        return None, match.group(1)

    query = parse_qs(parsed.query)
    bvid = (query.get('bvid') or [''])[0]
    aid = (query.get('aid') or [''])[0]
    return bvid or None, aid or None


def bilibili_cover_candidates(url: str) -> list[str]:
    bvid, aid = parse_bilibili_video_id(url)
    if not bvid and not aid:
        return []

    params = {'bvid': bvid} if bvid else {'aid': aid}
    try:
        response = http_requests.get(
            'https://api.bilibili.com/x/web-interface/view',
            params=params,
            headers=cover_fetch_headers(url, 'application/json,*/*;q=0.8'),
            timeout=15,
        )
        if response.status_code >= 400:
            return []
        data = response.json()
    except Exception as e:
        logger.debug(f"Bilibili cover API failed for {url}: {e}")
        return []

    pic = data.get('data', {}).get('pic') if isinstance(data, dict) else ''
    if not pic:
        return []
    return [urljoin('https://www.bilibili.com/', pic)]


def read_limited_response(response, limit: int) -> bytes:
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise ValueError("Remote content is too large.")
        chunks.append(chunk)
    return b''.join(chunks)


def try_fetch_image_url(url: str) -> bytes | None:
    validate_http_url(url)
    try:
        response = http_requests.get(
            url,
            headers=cover_fetch_headers(url),
            timeout=15,
            stream=True,
            allow_redirects=True,
        )
        if response.status_code >= 400:
            return None
        content_type = response.headers.get('Content-Type', '').lower()
        if content_type and not content_type.startswith('image/'):
            return None
        content_length = response.headers.get('Content-Length')
        if content_length and content_length.isdigit() and int(content_length) > COVER_IMAGE_MAX_BYTES:
            raise ValueError("Remote image is too large.")
        image_data = read_limited_response(response, COVER_IMAGE_MAX_BYTES)
    except ValueError:
        raise
    except Exception as e:
        logger.debug(f"Image URL fetch failed for {url}: {e}")
        return None

    if detect_image_mime(image_data):
        return image_data
    return None


def fetch_page_cover_url(url: str) -> str | None:
    validate_http_url(url)
    response = http_requests.get(
        url,
        headers=cover_fetch_headers(url, 'text/html,*/*;q=0.8'),
        timeout=15,
        stream=True,
        allow_redirects=True,
    )
    if response.status_code >= 400:
        return None

    content = read_limited_response(response, COVER_PAGE_MAX_BYTES)
    html = content.decode(response.encoding or 'utf-8', errors='replace')
    parser = CoverImageParser()
    parser.feed(html)
    for candidate in parser.urls:
        if candidate:
            return urljoin(response.url, candidate)
    return None


def fetch_cover_from_url(url: str) -> bytes:
    url = validate_http_url(url)

    image_data = try_fetch_image_url(url)
    if image_data:
        return image_data

    for candidate in youtube_cover_candidates(url):
        image_data = try_fetch_image_url(candidate)
        if image_data:
            return image_data

    for candidate in bilibili_cover_candidates(url):
        image_data = try_fetch_image_url(candidate)
        if image_data:
            return image_data

    cover_url = fetch_page_cover_url(url)
    if cover_url:
        image_data = try_fetch_image_url(cover_url)
        if image_data:
            return image_data

    raise ValueError("Could not find a JPEG or PNG cover image at that URL.")


def make_track_id(context: ContextTypes.DEFAULT_TYPE) -> str:
    uploads = context.user_data.setdefault('uploads', {})
    while True:
        track_id = secrets.token_hex(6)
        if track_id not in uploads:
            return track_id


def trim_recent_uploads(context: ContextTypes.DEFAULT_TYPE):
    uploads = context.user_data.setdefault('uploads', {})
    order = context.user_data.setdefault('upload_order', [])
    while len(order) > MAX_RECENT_UPLOADS:
        old_track_id = order.pop(0)
        uploads.pop(old_track_id, None)


def remember_upload(
    context: ContextTypes.DEFAULT_TYPE,
    path: Path,
    meta: dict,
    track_id: str | None = None,
    add_to_recent: bool = True,
) -> str:
    uploads = context.user_data.setdefault('uploads', {})
    order = context.user_data.setdefault('upload_order', [])
    track_id = track_id or make_track_id(context)
    existing = uploads.get(track_id, {})
    uploads[track_id] = {
        **existing,
        'path': str(path),
        'meta': {
            'artist': meta.get('artist', 'Unknown'),
            'title': meta.get('title', path.stem),
            'album': meta.get('album', ''),
            'genre': meta.get('genre', ''),
            'duration': meta.get('duration', 0),
        },
    }
    if add_to_recent:
        if track_id in order:
            order.remove(track_id)
        order.append(track_id)
        context.user_data['last_upload_id'] = track_id
        trim_recent_uploads(context)
    return track_id


def get_upload_record(context: ContextTypes.DEFAULT_TYPE, track_id: str | None = None):
    uploads = context.user_data.get('uploads', {})
    track_id = track_id or context.user_data.get('last_upload_id')
    if not track_id:
        return None, None

    upload = uploads.get(track_id)
    if not upload:
        return track_id, None

    path_text = upload.get('path', '')
    if not path_text:
        return track_id, None
    path = Path(path_text)
    if not path.exists():
        return track_id, None

    meta = get_metadata(path)
    if not meta:
        meta = upload.get('meta', {})
    upload['meta'] = {
        'artist': meta.get('artist', 'Unknown'),
        'title': meta.get('title', path.stem),
        'album': meta.get('album', ''),
        'genre': meta.get('genre', ''),
        'duration': meta.get('duration', upload.get('meta', {}).get('duration', 0)),
    }
    return track_id, {**upload, 'path': path, 'meta': upload['meta']}


def get_upload(context: ContextTypes.DEFAULT_TYPE, track_id: str | None = None):
    track_id, upload = get_upload_record(context, track_id)
    if not upload:
        return track_id, None, None
    return track_id, upload['path'], upload['meta']


def recent_uploads(context: ContextTypes.DEFAULT_TYPE, limit: int | None = None) -> list:
    order = context.user_data.get('upload_order', [])
    recent = []
    for track_id in reversed(order):
        track_id, path, meta = get_upload(context, track_id)
        if path and meta:
            recent.append((track_id, path, meta))
            if limit and len(recent) >= limit:
                break
    return recent


def remember_lyrics_results(context: ContextTypes.DEFAULT_TYPE, track_id: str, results: list) -> str:
    uploads = context.user_data.setdefault('uploads', {})
    upload = uploads.setdefault(track_id, {})
    lyrics_sets = upload.setdefault('lyrics_sets', {})
    lyrics_id = secrets.token_hex(3)
    lyrics_sets[lyrics_id] = results
    while len(lyrics_sets) > MAX_LYRICS_SETS_PER_UPLOAD:
        lyrics_sets.pop(next(iter(lyrics_sets)))
    return lyrics_id


def get_lyrics_results(context: ContextTypes.DEFAULT_TYPE, track_id: str, lyrics_id: str) -> list:
    uploads = context.user_data.get('uploads', {})
    upload = uploads.get(track_id, {})
    lyrics_sets = upload.get('lyrics_sets', {})
    return lyrics_sets.get(lyrics_id, [])


def format_track(path: Path, meta: dict) -> str:
    return (
        f"🎵 {meta.get('artist', 'Unknown')} - {meta.get('title', path.stem)}\n"
        f"💿 {meta.get('album') or '(no album)'}\n"
        f"🏷 {meta.get('genre') or '(no genre)'}\n"
        f"📁 {path}"
    )


def build_edit_keyboard(track_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Artist", callback_data=f"edit_field_{track_id}_artist"),
            InlineKeyboardButton("Title", callback_data=f"edit_field_{track_id}_title"),
        ],
        [
            InlineKeyboardButton("Album", callback_data=f"edit_field_{track_id}_album"),
            InlineKeyboardButton("Genre", callback_data=f"edit_field_{track_id}_genre"),
        ],
        [
            InlineKeyboardButton("Cover", callback_data=f"edit_cover_{track_id}"),
            InlineKeyboardButton("Lyrics", callback_data=f"edit_lyrics_{track_id}"),
        ],
        [InlineKeyboardButton("Choose another track", callback_data="edit_list")],
        [InlineKeyboardButton("Done", callback_data="edit_done")],
    ])


def build_upload_choices_keyboard(uploads: list) -> InlineKeyboardMarkup:
    buttons = []
    for track_id, _path, meta in uploads:
        label = f"{meta.get('artist', 'Unknown')} - {meta.get('title', 'Unknown')}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"edit_pick_{track_id}")])
    buttons.append([InlineKeyboardButton("Done", callback_data="edit_done")])
    return InlineKeyboardMarkup(buttons)


def build_recent_uploads_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    return build_upload_choices_keyboard(recent_uploads(context, MAX_EDIT_UPLOAD_CHOICES))


def normalize_search_text(text: str) -> str:
    text = unicodedata.normalize('NFKC', str(text))
    return re.sub(r'\s+', ' ', text.casefold()).strip()


def compact_search_text(text: str) -> str:
    return re.sub(r'\s+', '', normalize_search_text(text))


def score_library_match(query: str, path: Path, meta: dict) -> int:
    query = normalize_search_text(query)
    title = normalize_search_text(meta.get('title', ''))
    artist = normalize_search_text(meta.get('artist', ''))
    album = normalize_search_text(meta.get('album', ''))
    genre = normalize_search_text(meta.get('genre', ''))
    stem = normalize_search_text(path.stem)
    try:
        relative_path = normalize_search_text(str(path.relative_to(MUSIC_FOLDER)))
    except ValueError:
        relative_path = normalize_search_text(str(path))
    combined = normalize_search_text(' '.join([artist, title, album, genre, stem, relative_path]))
    compact_query = compact_search_text(query)
    compact_title = compact_search_text(title)
    compact_stem = compact_search_text(stem)
    compact_artist_title = compact_search_text(f"{artist} {title}")
    compact_relative_path = compact_search_text(relative_path)

    if not query:
        return 0
    if query == title:
        return 1000
    if query == stem:
        return 900
    if query == f"{artist} {title}".strip() or query == f"{artist} - {title}".strip():
        return 850
    if query in title:
        return 800
    if query in stem:
        return 700
    if query in f"{artist} {title}".strip():
        return 650
    if query in relative_path:
        return 500
    if compact_query and compact_query == compact_title:
        return 780
    if compact_query and compact_query == compact_stem:
        return 680
    if compact_query and compact_query in compact_artist_title:
        return 620
    if compact_query and compact_query in compact_relative_path:
        return 480

    tokens = [token for token in query.split(' ') if token]
    if tokens and all(token in combined for token in tokens):
        return 300 + sum(1 for token in tokens if token in title) * 20
    return 0


def search_library(query: str, limit: int = MAX_LIBRARY_SEARCH_RESULTS) -> list:
    matches = []
    if not query or not MUSIC_FOLDER.exists():
        return matches

    for path in MUSIC_FOLDER.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_AUDIO_EXTS:
            continue
        try:
            meta = get_metadata(path)
        except Exception as e:
            logger.debug(f"Library search skipped {path}: {e}")
            continue
        if not meta:
            continue
        score = score_library_match(query, path, meta)
        if score:
            matches.append((score, path, meta))

    matches.sort(key=lambda item: (-item[0], str(item[1]).casefold()))
    return [(path, meta) for _score, path, meta in matches[:limit]]


def clear_library_search_uploads(context: ContextTypes.DEFAULT_TYPE):
    uploads = context.user_data.setdefault('uploads', {})
    order = set(context.user_data.setdefault('upload_order', []))
    for track_id in context.user_data.get('library_search_upload_ids', []):
        if track_id not in order and track_id != context.user_data.get('active_edit_upload_id'):
            uploads.pop(track_id, None)
    context.user_data['library_search_upload_ids'] = []


def remember_library_search_results(context: ContextTypes.DEFAULT_TYPE, matches: list) -> list:
    clear_library_search_uploads(context)
    choices = []
    search_ids = []
    for path, meta in matches:
        track_id = remember_upload(context, path, meta, add_to_recent=False)
        choices.append((track_id, path, meta))
        search_ids.append(track_id)
    context.user_data['library_search_upload_ids'] = search_ids
    return choices


def find_synced_lyrics(meta: dict) -> list:
    """Search all sources and return deduplicated synced lyrics candidates."""
    candidates = search_all_sources(
        meta.get('title', ''),
        meta.get('artist', ''),
        meta.get('duration', 0),
    )
    results = []
    for source, info, fetcher in candidates:
        try:
            lrc = fetcher()
            if lrc and re.search(r'\[\d{1,2}:\d{2}\.\d{2,3}\]', lrc):
                results.append((source, info, lrc))
        except Exception as e:
            logger.debug(f"Lyrics fetch failed: {e}")

    seen = set()
    unique = []
    for source, info, lrc in results:
        key = lrc[:200]
        if key not in seen:
            seen.add(key)
            unique.append((source, info, lrc))
    return unique[:6]


async def show_edit_menu(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    track_id: str | None = None,
    prefix: str = "✏️ Editing upload",
):
    track_id, path, meta = get_upload(context, track_id)
    if meta is None:
        await message.reply_text("⚠️ No recent upload to edit. Send an audio file first.")
        return

    context.user_data['active_edit_upload_id'] = track_id
    await message.reply_text(
        f"{prefix}\n\n{format_track(path, meta)}",
        reply_markup=build_edit_keyboard(track_id),
    )


async def show_recent_uploads(message, context: ContextTypes.DEFAULT_TYPE):
    uploads = recent_uploads(context, MAX_EDIT_UPLOAD_CHOICES)
    if not uploads:
        await message.reply_text("⚠️ No recent upload to edit. Send an audio file first.")
        return

    if len(uploads) == 1:
        track_id, _path, _meta = uploads[0]
        await show_edit_menu(message, context, track_id)
        return

    lines = ["✏️ Choose a recent upload to edit:\n"]
    for i, (_track_id, _path, meta) in enumerate(uploads, 1):
        lines.append(f"{i}. {meta.get('artist', 'Unknown')} - {meta.get('title', 'Unknown')}")
    await message.reply_text(
        '\n'.join(lines),
        reply_markup=build_upload_choices_keyboard(uploads),
    )


async def show_library_search(message, context: ContextTypes.DEFAULT_TYPE, query: str):
    await message.reply_text(f"🔎 Searching library for: {query}")
    matches = await asyncio.to_thread(search_library, query)
    if not matches:
        await message.reply_text(f"⚠️ No tracks found for: {query}")
        return

    choices = remember_library_search_results(context, matches)
    if len(choices) == 1:
        track_id, path, meta = choices[0]
        remember_upload(context, path, meta, track_id)
        await show_edit_menu(message, context, track_id, prefix=f"✏️ Editing library match for: {query}")
        return

    lines = [f"✏️ Choose a library match for: {query}\n"]
    for i, (_track_id, _path, meta) in enumerate(choices, 1):
        lines.append(f"{i}. {meta.get('artist', 'Unknown')} - {meta.get('title', 'Unknown')}")
    await message.reply_text(
        '\n'.join(lines),
        reply_markup=build_upload_choices_keyboard(choices),
    )


async def download_cover_image(update: Update) -> bytes | None:
    msg = update.message
    image_file = None
    suffix = '.jpg'

    if msg.photo:
        image_file = msg.photo[-1]
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith('image/'):
        image_file = msg.document
        suffix = Path(msg.document.file_name or '').suffix or '.img'

    if image_file is None:
        return None

    tmp_path = Path(tempfile.gettempdir()) / f"navidrome_cover_{secrets.token_hex(8)}{suffix}"
    try:
        tg_file = await image_file.get_file()
        await tg_file.download_to_drive(str(tmp_path))
        return tmp_path.read_bytes()
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


async def apply_cover_data(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict, image_data: bytes):
    track_id = pending.get('track_id')
    track_id, path, meta = get_upload(context, track_id)
    if meta is None:
        context.user_data.pop('edit_pending', None)
        await update.message.reply_text("⚠️ This upload no longer exists.")
        return

    if len(image_data) > COVER_IMAGE_MAX_BYTES:
        await update.message.reply_text("⚠️ Cover image is too large.")
        return

    mime = detect_image_mime(image_data)
    if mime is None:
        await update.message.reply_text("⚠️ Cover must be a JPEG or PNG image.")
        return

    try:
        write_cover(path, image_data, mime)
    except Exception as e:
        logger.error(f"Cover edit failed: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Cover edit failed: {e}")
        return

    context.user_data.pop('edit_pending', None)
    scan_ok = trigger_scan()
    scan_status = "✓ Scan triggered" if scan_ok else "⚠️ Scan trigger failed"
    if scan_ok:
        schedule_post_hook(str(path))
    await update.message.reply_text(
        f"✅ Cover updated.\n"
        f"🔄 {scan_status}\n\n"
        f"{format_track(path, meta)}",
        reply_markup=build_edit_keyboard(track_id),
    )


async def handle_cover_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict):
    image_data = await download_cover_image(update)
    if not image_data:
        await update.message.reply_text("⚠️ Send a JPEG/PNG image or a supported URL for the cover.")
        return
    await apply_cover_data(update, context, pending, image_data)


async def handle_cover_text(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict):
    url = extract_first_url(update.message.text or '')
    if not url:
        await update.message.reply_text("⚠️ Send a JPEG/PNG image, image URL, or YouTube/niconico/bilibili URL.")
        return

    await update.message.reply_text("🔎 Fetching cover image from URL...")
    try:
        image_data = await asyncio.to_thread(fetch_cover_from_url, url)
    except Exception as e:
        logger.warning(f"Cover URL fetch failed: {e}")
        await update.message.reply_text(f"❌ Could not fetch cover: {e}")
        return

    await apply_cover_data(update, context, pending, image_data)


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming audio/document files."""
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return

    msg = update.message
    pending = context.user_data.get('edit_pending')
    if pending and pending.get('kind') == 'cover':
        await handle_cover_upload(update, context, pending)
        return

    if msg.photo:
        return

    context.user_data.pop('edit_pending', None)

    # Get file object (audio or document)
    if msg.audio:
        file_obj = msg.audio
        file_name = msg.audio.file_name or f"{msg.audio.title or 'audio'}.mp3"
    elif msg.document:
        file_name = msg.document.file_name or 'unknown'
        ext = Path(file_name).suffix.lower()
        if ext not in SUPPORTED_AUDIO_EXTS:
            await update.message.reply_text("⚠️ Not a supported audio format.")
            return
        file_obj = msg.document
    else:
        return

    # Check file size (Telegram Bot API limit: 20MB)
    file_size_mb = file_obj.file_size / (1024 * 1024) if file_obj.file_size else 0
    if file_size_mb > 20:
        if not pyro_client:
            await msg.reply_text(f"❌ File too large ({file_size_mb:.1f}MB). Set API_ID/API_HASH in .env for large file support.")
            return
        # Download via Pyrogram (MTProto, up to 2GB)
        await msg.reply_text(f"⏬ Downloading large file via MTProto: {file_name} ({file_size_mb:.1f}MB)")
        tmp_path = Path(tempfile.gettempdir()) / file_name
        async with pyro_client:
            pyro_msg = await pyro_client.get_messages(msg.chat_id, msg.message_id)
            await pyro_msg.download(file_name=str(tmp_path))
    else:
        await msg.reply_text(f"⏬ Downloading: {file_name}")
        tg_file = await file_obj.get_file()
        tmp_path = Path(tempfile.gettempdir()) / file_name
        await tg_file.download_to_drive(str(tmp_path))

    # Read metadata
    meta = get_metadata(tmp_path)
    ext = tmp_path.suffix

    # Determine destination
    dest_path = determine_path(meta, ext)

    # Move to music folder
    shutil.move(str(tmp_path), str(dest_path))

    # Trigger scan
    scan_ok = trigger_scan()
    scan_status = "✓ Scan triggered" if scan_ok else "⚠️ Scan trigger failed"
    if scan_ok:
        schedule_post_hook(str(dest_path))

    await msg.reply_text(
        f"✅ Saved!\n"
        f"🎵 {meta['artist']} - {meta['title']}\n"
        f"💿 {meta['album'] or '(no album)'}\n"
        f"🏷 {meta.get('genre') or '(no genre)'}\n"
        f"📁 {dest_path}\n"
        f"🔄 {scan_status}"
    )
    track_id = remember_upload(context, dest_path, meta)
    logger.info(f"User {user.id} uploaded: {dest_path}")

    # Search lyrics if missing
    if not has_synced_lyrics(dest_path):
        await msg.reply_text("🔍 Searching lyrics...")
        results = find_synced_lyrics(meta)

        if not results:
            await msg.reply_text("❌ No synced lyrics found.")
            return

        lyrics_id = remember_lyrics_results(context, track_id, results)

        # Build inline keyboard
        buttons = []
        text_parts = ["🎤 Choose lyrics:\n"]
        for i, (source, info, lrc) in enumerate(results):
            preview = preview_lrc(lrc, 3).replace('\n', '\n   ')
            text_parts.append(f"{i+1}. ({source}) {info.get('artist','')} - {info.get('title','')}\n   {preview}\n")
            buttons.append([InlineKeyboardButton(
                f"{i+1}. {source}: {info.get('title','')[:30]}",
                callback_data=f"lrc_{track_id}_{lyrics_id}_{i}",
            )])
        buttons.append([InlineKeyboardButton("⏭ Skip", callback_data=f"lrc_skip_{track_id}_{lyrics_id}")])

        await msg.reply_text('\n'.join(text_parts), reply_markup=InlineKeyboardMarkup(buttons))


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start interactive editing for the last uploaded track."""
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return

    context.user_data.pop('edit_pending', None)
    query = ' '.join(context.args).strip()
    if query:
        await show_library_search(update.message, context, query)
        return

    await show_recent_uploads(update.message, context)


async def handle_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the next text message as an edit field value."""
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return

    pending = context.user_data.get('edit_pending')
    if not pending:
        return
    if pending.get('kind') == 'cover':
        await handle_cover_text(update, context, pending)
        return

    field = pending.get('field')
    track_id = pending.get('track_id')
    if field not in EDIT_FIELD_LABELS:
        context.user_data.pop('edit_pending', None)
        return

    value = update.message.text.strip()
    if field in ('artist', 'title') and not value:
        await update.message.reply_text(f"⚠️ {EDIT_FIELD_LABELS[field]} cannot be empty.")
        return
    if field in ('album', 'genre') and value == '-':
        value = ''

    track_id, path, meta = get_upload(context, track_id)
    if meta is None:
        context.user_data.pop('edit_pending', None)
        await update.message.reply_text("⚠️ This upload no longer exists.")
        return

    old_path = path
    new_meta = dict(meta)
    new_meta[field] = value
    new_path = determine_path(new_meta, old_path.suffix)

    if new_path.resolve() != old_path.resolve() and new_path.exists():
        await update.message.reply_text(f"⚠️ Target file already exists:\n{new_path}")
        return

    try:
        write_metadata(old_path, new_meta)
        final_path = old_path
        if new_path.resolve() != old_path.resolve():
            shutil.move(str(old_path), str(new_path))
            final_path = new_path
            try:
                old_path.parent.rmdir()
            except OSError:
                pass
    except Exception as e:
        logger.error(f"Metadata edit failed: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Edit failed: {e}")
        return

    context.user_data.pop('edit_pending', None)
    remember_upload(context, final_path, new_meta, track_id)

    scan_ok = trigger_scan()
    scan_status = "✓ Scan triggered" if scan_ok else "⚠️ Scan trigger failed"
    if scan_ok:
        schedule_post_hook(str(final_path))
    await update.message.reply_text(
        f"✅ Updated {EDIT_FIELD_LABELS[field]}.\n"
        f"🔄 {scan_status}\n\n"
        f"{format_track(final_path, new_meta)}",
        reply_markup=build_edit_keyboard(track_id),
    )


async def handle_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle interactive edit buttons."""
    query = update.callback_query
    user = query.from_user
    await query.answer()

    if not is_allowed(user.id):
        await query.edit_message_text("⛔ Not authorized.")
        return

    data = query.data

    if data == "edit_done":
        context.user_data.pop('edit_pending', None)
        await query.edit_message_text("✅ Edit finished.")
        return

    if data == "edit_list":
        context.user_data.pop('edit_pending', None)
        uploads = recent_uploads(context, MAX_EDIT_UPLOAD_CHOICES)
        if not uploads:
            await query.edit_message_text("⚠️ No recent upload to edit. Send an audio file first.")
            return
        lines = ["✏️ Choose a recent upload to edit:\n"]
        for i, (_track_id, _path, meta) in enumerate(uploads, 1):
            lines.append(f"{i}. {meta.get('artist', 'Unknown')} - {meta.get('title', 'Unknown')}")
        await query.edit_message_text(
            '\n'.join(lines),
            reply_markup=build_recent_uploads_keyboard(context),
        )
        return

    if data.startswith("edit_pick_"):
        track_id = data.removeprefix("edit_pick_")
        track_id, path, meta = get_upload(context, track_id)
        if meta is None:
            await query.edit_message_text("⚠️ This upload no longer exists.")
            return
        remember_upload(context, path, meta, track_id)
        context.user_data['active_edit_upload_id'] = track_id
        await query.edit_message_text(
            f"✏️ Editing upload\n\n{format_track(path, meta)}",
            reply_markup=build_edit_keyboard(track_id),
        )
        return

    if data.startswith("edit_cover_"):
        track_id = data.removeprefix("edit_cover_")
        track_id, path, meta = get_upload(context, track_id)
        if meta is None:
            await query.edit_message_text("⚠️ This upload no longer exists.")
            return

        context.user_data['active_edit_upload_id'] = track_id
        context.user_data['edit_pending'] = {'kind': 'cover', 'track_id': track_id}
        await query.edit_message_text(
            f"🖼 Editing Cover\n\n"
            f"{format_track(path, meta)}\n\n"
            f"Send a JPEG/PNG image, image URL, or YouTube/niconico/bilibili URL."
        )
        return

    if data.startswith("edit_field_"):
        try:
            _prefix, _action, track_id, field = data.split('_', 3)
        except ValueError:
            await query.edit_message_text("⚠️ Unknown field.")
            return
        if field not in EDIT_FIELD_LABELS:
            await query.edit_message_text("⚠️ Unknown field.")
            return

        track_id, path, meta = get_upload(context, track_id)
        if meta is None:
            await query.edit_message_text("⚠️ This upload no longer exists.")
            return

        context.user_data['active_edit_upload_id'] = track_id
        context.user_data['edit_pending'] = {'kind': 'field', 'field': field, 'track_id': track_id}
        hint = f"Send '-' to clear the {field}." if field in ('album', 'genre') else "Send the new value."
        await query.edit_message_text(
            f"✏️ Editing {EDIT_FIELD_LABELS[field]}\n\n"
            f"{format_track(path, meta)}\n\n"
            f"{hint}"
        )
        return

    if data.startswith("edit_lyrics_"):
        track_id = data.removeprefix("edit_lyrics_")
        track_id, path, meta = get_upload(context, track_id)
        if meta is None:
            await query.edit_message_text("⚠️ This upload no longer exists.")
            return

        context.user_data['active_edit_upload_id'] = track_id
        await query.edit_message_text(
            f"🔍 Searching lyrics again...\n\n"
            f"{format_track(path, meta)}"
        )
        results = find_synced_lyrics(meta)
        if not results:
            await query.edit_message_text("❌ No synced lyrics found.")
            return

        lyrics_id = remember_lyrics_results(context, track_id, results)

        buttons = []
        text_parts = ["🎤 Choose replacement lyrics:\n"]
        for i, (source, info, lrc) in enumerate(results):
            preview = preview_lrc(lrc, 3).replace('\n', '\n   ')
            text_parts.append(f"{i+1}. ({source}) {info.get('artist','')} - {info.get('title','')}\n   {preview}\n")
            buttons.append([InlineKeyboardButton(
                f"{i+1}. {source}: {info.get('title','')[:30]}",
                callback_data=f"edit_lrc_{track_id}_{lyrics_id}_{i}",
            )])
        buttons.append([InlineKeyboardButton("Back", callback_data=f"edit_back_{track_id}")])

        await query.edit_message_text(
            '\n'.join(text_parts),
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("edit_back_"):
        context.user_data.pop('edit_pending', None)
        track_id = data.removeprefix("edit_back_")
        track_id, path, meta = get_upload(context, track_id)
        if meta is None:
            await query.edit_message_text("⚠️ This upload no longer exists.")
            return
        await query.edit_message_text(
            f"✏️ Editing upload\n\n{format_track(path, meta)}",
            reply_markup=build_edit_keyboard(track_id),
        )
        return

    if data.startswith("edit_lrc_"):
        try:
            _prefix, _action, track_id, lyrics_id, idx_text = data.split('_', 4)
            idx = int(idx_text)
        except ValueError:
            await query.edit_message_text("⚠️ Session expired.")
            return

        results = get_lyrics_results(context, track_id, lyrics_id)
        track_id, path, meta = get_upload(context, track_id)
        if idx >= len(results) or not path or meta is None:
            await query.edit_message_text("⚠️ Session expired.")
            return

        source, info, lrc = results[idx]
        write_lyrics(path, lrc)
        scan_ok = trigger_scan()
        scan_status = "✓ Scan triggered" if scan_ok else "⚠️ Scan trigger failed"
        if scan_ok:
            schedule_post_hook(str(path))
        await query.edit_message_text(
            f"✅ Lyrics replaced! ({source}: {info.get('title','')})\n"
            f"🔄 {scan_status}",
            reply_markup=build_edit_keyboard(track_id),
        )
        return

    await query.edit_message_text("⚠️ Unknown edit action.")


async def handle_lyrics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle lyrics selection callback."""
    query = update.callback_query
    await query.answer()

    if not is_allowed(query.from_user.id):
        await query.edit_message_text("⛔ Not authorized.")
        return

    data = query.data
    if data.startswith("lrc_skip_"):
        await query.edit_message_text("⏭ Lyrics skipped.")
        return

    try:
        _prefix, track_id, lyrics_id, idx_text = data.split('_', 3)
        idx = int(idx_text)
    except ValueError:
        await query.edit_message_text("⚠️ Session expired.")
        return

    results = get_lyrics_results(context, track_id, lyrics_id)
    track_id, path, meta = get_upload(context, track_id)
    if idx >= len(results) or not path or meta is None:
        await query.edit_message_text("⚠️ Session expired.")
        return

    source, info, lrc = results[idx]
    write_lyrics(path, lrc)
    scan_ok = trigger_scan()
    scan_status = "✓ Scan triggered" if scan_ok else "⚠️ Scan trigger failed"
    if scan_ok:
        schedule_post_hook(str(path))
    await query.edit_message_text(
        f"✅ Lyrics saved! ({source}: {info.get('title','')})\n"
        f"🔄 {scan_status}"
    )


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Navidrome Upload Bot\n\n"
        "Send me an audio file and I'll add it to your library.\n"
        "Use /edit after uploading to edit metadata or replace lyrics.\n"
        "Supported: mp3, flac, m4a, ogg, wav, aac, wma, opus"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling update: {context.error}", exc_info=context.error)


async def register_bot_commands(application: Application):
    """Register commands shown in Telegram's command menu."""
    commands = [
        BotCommand("start", "Show help"),
        BotCommand("edit", "Edit recent uploads"),
    ]
    try:
        await application.bot.set_my_commands(
            commands,
            scope=BotCommandScopeAllPrivateChats(),
        )
        logger.info("Bot commands registered")
    except Exception as e:
        logger.error(f"Bot command registration failed: {e}")


def main():
    builder = Application.builder().token(BOT_TOKEN)
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
    builder = builder.post_init(register_bot_commands)
    app = builder.build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("edit", handle_edit))
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.ALL | filters.PHOTO, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_text))
    app.add_handler(CallbackQueryHandler(handle_edit_callback, pattern=r'^edit_'))
    app.add_handler(CallbackQueryHandler(handle_lyrics_callback, pattern=r'^lrc_'))
    logger.info("Bot started")
    app.run_polling()


if __name__ == '__main__':
    main()
