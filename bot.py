"""
Telegram bot: receive audio files, process metadata, save to Navidrome music folder, trigger scan.
"""
import os
import re
import logging
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip('. ')


def determine_path(meta: dict, ext: str) -> Path:
    """Determine destination path: {MUSIC_FOLDER}/{Artist}/{Title}.ext"""
    artist = sanitize_filename(meta.get('artist', 'Unknown'))
    title = sanitize_filename(meta.get('title', 'Unknown'))
    dest_dir = MUSIC_FOLDER / artist
    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir / f"{title}{ext}"


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming audio/document files."""
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return

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
    logger.info(f"User {user.id} uploaded: {dest_path}")

    # Search lyrics if missing
    if not has_synced_lyrics(dest_path):
        await msg.reply_text("🔍 Searching lyrics...")
        candidates = search_all_sources(meta['title'], meta['artist'], meta.get('duration', 0))
        # Fetch and filter synced results
        results = []
        for source, info, fetcher in candidates:
            try:
                lrc = fetcher()
                if lrc and re.search(r'\[\d{1,2}:\d{2}\.\d{2,3}\]', lrc):
                    results.append((source, info, lrc))
            except Exception:
                pass
        # Deduplicate
        seen = set()
        unique = []
        for source, info, lrc in results:
            key = lrc[:200]
            if key not in seen:
                seen.add(key)
                unique.append((source, info, lrc))
        results = unique[:6]

        if not results:
            await msg.reply_text("❌ No synced lyrics found.")
            return

        # Store results in context for callback
        context.user_data['lyrics_results'] = results
        context.user_data['lyrics_dest'] = str(dest_path)

        # Build inline keyboard
        buttons = []
        text_parts = ["🎤 Choose lyrics:\n"]
        for i, (source, info, lrc) in enumerate(results):
            preview = preview_lrc(lrc, 3).replace('\n', '\n   ')
            text_parts.append(f"{i+1}. ({source}) {info.get('artist','')} - {info.get('title','')}\n   {preview}\n")
            buttons.append([InlineKeyboardButton(f"{i+1}. {source}: {info.get('title','')[:30]}", callback_data=f"lrc_{i}")])
        buttons.append([InlineKeyboardButton("⏭ Skip", callback_data="lrc_skip")])

        await msg.reply_text('\n'.join(text_parts), reply_markup=InlineKeyboardMarkup(buttons))


async def handle_lyrics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle lyrics selection callback."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "lrc_skip":
        await query.edit_message_text("⏭ Lyrics skipped.")
        return

    idx = int(data.split('_')[1])
    results = context.user_data.get('lyrics_results', [])
    dest_path = context.user_data.get('lyrics_dest', '')

    if idx >= len(results) or not dest_path:
        await query.edit_message_text("⚠️ Session expired.")
        return

    source, info, lrc = results[idx]
    write_lyrics(Path(dest_path), lrc)
    await query.edit_message_text(f"✅ Lyrics saved! ({source}: {info.get('title','')})")


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Navidrome Upload Bot\n\n"
        "Send me an audio file and I'll add it to your library.\n"
        "Supported: mp3, flac, m4a, ogg, wav, aac, wma, opus"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling update: {context.error}", exc_info=context.error)

def main():
    builder = Application.builder().token(BOT_TOKEN)
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
    app = builder.build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.ALL, handle_audio))
    app.add_handler(CallbackQueryHandler(handle_lyrics_callback, pattern=r'^lrc_'))
    logger.info("Bot started")
    app.run_polling()


if __name__ == '__main__':
    main()
