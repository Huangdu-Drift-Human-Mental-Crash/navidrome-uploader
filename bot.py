"""
Telegram bot: receive audio files, process metadata, save to Navidrome music folder, trigger scan.
"""
import os
import re
import logging
import secrets
from pathlib import Path

import requests as http_requests
from dotenv import load_dotenv
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TIT2, TPE1, TALB
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
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
API_ID = os.getenv('API_ID', '').strip()
API_HASH = os.getenv('API_HASH', '').strip()

# Pyrogram client for large file downloads (>20MB)
pyro_client = None
if API_ID and API_HASH:
    from pyrogram import Client as PyroClient
    proxy_cfg = None
    if PROXY_URL:
        from urllib.parse import urlparse
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
}
MAX_RECENT_UPLOADS = 50
MAX_EDIT_UPLOAD_CHOICES = 10
MAX_LYRICS_SETS_PER_UPLOAD = 5


def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


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
    meta = {'artist': 'Unknown', 'title': filepath.stem, 'album': ''}
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

    # Vorbis/FLAC
    if hasattr(tags, 'get'):
        if meta['artist'] == 'Unknown':
            meta['artist'] = (tags.get('artist') or tags.get('ARTIST') or ['Unknown'])[0]
        if meta['title'] == filepath.stem:
            meta['title'] = (tags.get('title') or tags.get('TITLE') or [filepath.stem])[0]
        if not meta['album']:
            meta['album'] = (tags.get('album') or tags.get('ALBUM') or [''])[0]

    # MP4
    if isinstance(audio, MP4):
        meta['artist'] = (audio.tags.get('\xa9ART') or ['Unknown'])[0]
        meta['title'] = (audio.tags.get('\xa9nam') or [filepath.stem])[0]
        meta['album'] = (audio.tags.get('\xa9alb') or [''])[0]

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
    ext = filepath.suffix.lower()

    if ext == '.mp3':
        tags = audio.tags or ID3()
        tags.delall('TPE1')
        tags.delall('TIT2')
        tags.delall('TALB')
        tags.add(TPE1(encoding=3, text=artist))
        tags.add(TIT2(encoding=3, text=title))
        if album:
            tags.add(TALB(encoding=3, text=album))
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
        audio.save()
        return

    raise ValueError(f"Unsupported metadata format: {ext}")


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


def remember_upload(context: ContextTypes.DEFAULT_TYPE, path: Path, meta: dict, track_id: str | None = None) -> str:
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
            'duration': meta.get('duration', 0),
        },
    }
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
            InlineKeyboardButton("Lyrics", callback_data=f"edit_lyrics_{track_id}"),
        ],
        [InlineKeyboardButton("Choose another track", callback_data="edit_list")],
        [InlineKeyboardButton("Done", callback_data="edit_done")],
    ])


def build_recent_uploads_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    buttons = []
    for track_id, _path, meta in recent_uploads(context, MAX_EDIT_UPLOAD_CHOICES):
        label = f"{meta.get('artist', 'Unknown')} - {meta.get('title', 'Unknown')}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"edit_pick_{track_id}")])
    buttons.append([InlineKeyboardButton("Done", callback_data="edit_done")])
    return InlineKeyboardMarkup(buttons)


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
        reply_markup=build_recent_uploads_keyboard(context),
    )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming audio/document files."""
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return
    context.user_data.pop('edit_pending', None)

    msg = update.message
    # Get file object (audio or document)
    if msg.audio:
        file_obj = msg.audio
        file_name = msg.audio.file_name or f"{msg.audio.title or 'audio'}.mp3"
    elif msg.document:
        file_name = msg.document.file_name or 'unknown'
        ext = Path(file_name).suffix.lower()
        if ext not in ('.mp3', '.flac', '.m4a', '.ogg', '.wav', '.aac', '.wma', '.opus'):
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

    await msg.reply_text(
        f"✅ Saved!\n"
        f"🎵 {meta['artist']} - {meta['title']}\n"
        f"💿 {meta['album'] or '(no album)'}\n"
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

    field = pending.get('field')
    track_id = pending.get('track_id')
    if field not in EDIT_FIELD_LABELS:
        context.user_data.pop('edit_pending', None)
        return

    value = update.message.text.strip()
    if field in ('artist', 'title') and not value:
        await update.message.reply_text(f"⚠️ {EDIT_FIELD_LABELS[field]} cannot be empty.")
        return
    if field == 'album' and value == '-':
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
        context.user_data['active_edit_upload_id'] = track_id
        await query.edit_message_text(
            f"✏️ Editing upload\n\n{format_track(path, meta)}",
            reply_markup=build_edit_keyboard(track_id),
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
        context.user_data['edit_pending'] = {'field': field, 'track_id': track_id}
        hint = "Send '-' to clear the album." if field == 'album' else "Send the new value."
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
    await query.edit_message_text(f"✅ Lyrics saved! ({source}: {info.get('title','')})")


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
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.ALL, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_text))
    app.add_handler(CallbackQueryHandler(handle_edit_callback, pattern=r'^edit_'))
    app.add_handler(CallbackQueryHandler(handle_lyrics_callback, pattern=r'^lrc_'))
    logger.info("Bot started")
    app.run_polling()


if __name__ == '__main__':
    main()
