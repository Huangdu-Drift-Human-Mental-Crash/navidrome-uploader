# Navidrome Uploader Bot

Upload audio files to [Navidrome](https://www.navidrome.org/) via Telegram, with automatic metadata parsing, synced lyrics search, and library scanning.

## Features

- Receives MP3 / FLAC / M4A / OGG / WAV etc. via Telegram
- Extracts ID3 / Vorbis / MP4 tags, organizes as `{Artist}/{Title}`
- Triggers Navidrome library scan via Subsonic API
- Searches QQ Music / Kugou / Netease for synced lyrics and embeds them
- `/edit` interactively edits recent uploads' artist/title/album/genre/cover and can replace lyrics
- Imports songs from a local folder on demand with `/import`

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)

## Installation

```bash
git clone https://github.com/Huangdu-Drift-Human-Mental-Crash/navidrome-uploader.git
cd navidrome-uploader
uv venv
uv pip install -p .venv/bin/python -r requirements.txt   # Linux/macOS
```

## Configuration

Copy `.env.example` to `.env` and fill in:

```ini
BOT_TOKEN=your_telegram_bot_token
ALLOWED_USERS=123456789,987654321
PROXY_URL=                    # optional, e.g. socks5://127.0.0.1:1080

NAVIDROME_URL=http://localhost:4533
NAVIDROME_USER=admin
NAVIDROME_PASS=your_password
MUSIC_FOLDER=/path/to/your/music/library

LOCAL_IMPORT_FOLDER=/path/to/incoming
LOCAL_IMPORT_SETTLE_SECONDS=10
LOCAL_IMPORT_TRANSCODE_ENABLED=false
LOCAL_IMPORT_TRANSCODE_THRESHOLD_KBPS=320
LOCAL_IMPORT_AAC_BITRATE_KBPS=256
LOCAL_IMPORT_AAC_ENCODER=auto
# LOCAL_IMPORT_TRANSCODE_TIMEOUT=1800
```

## Running

```bash
python bot.py                         # Linux/macOS
.venv\Scripts\python.exe bot.py     # Windows
```

## Directory structure

Files are stored as `{MUSIC_FOLDER}/{Artist}/{Title}.ext` (flat, no album subdirectories).

## Local folder import

Send `/import` to the bot to scan `LOCAL_IMPORT_FOLDER` recursively. Supported
audio files are moved into `MUSIC_FOLDER` using the same metadata and
destination-path logic as Telegram uploads. The command reports the imported and
failed file counts. A single Navidrome scan is triggered after each non-empty
batch, followed by `POST_HOOK` for every successfully imported track.

Successfully imported tracks are added to the invoking user's recent `/edit`
list. Tracks without embedded synced lyrics also run through the same lyrics
search and interactive selection flow used by Telegram uploads. Batch lyrics
search runs in a serialized background queue so other bot commands and lyrics
buttons remain responsive.

After a file is safely written into the music library, its source file is deleted
from the incoming folder. If deleting the source fails, the new library file is
rolled back and the source is retried later. Existing library paths are never
overwritten; a numeric suffix such as `Title (2).flac` is used instead. Files
newer than `LOCAL_IMPORT_SETTLE_SECONDS` are skipped until a later scan to avoid
importing partially copied downloads.

`LOCAL_IMPORT_FOLDER` and `MUSIC_FOLDER` must be separate, non-overlapping
directories.

### Optional AAC transcoding

Set `LOCAL_IMPORT_TRANSCODE_ENABLED=true` to transcode local imports whose
reported audio bitrate is higher than `LOCAL_IMPORT_TRANSCODE_THRESHOLD_KBPS`.
The default threshold is 320 kbps and the output is AAC at 256 kbps in an
`.m4a` container. Metadata is retained, and compatible embedded cover art is
copied. The source file is deleted only after the M4A is successfully written.

With `LOCAL_IMPORT_AAC_ENCODER=auto`, `qaac` is preferred and uses Apple AAC
CVBR with the configured bitrate and encoder quality 2. If qaac is unavailable,
FFmpeg's `aac_at` CVBR encoder is tried next. Other systems fall back to
FFmpeg's native `aac` encoder at the configured bitrate; native AAC uses CBR
rather than true CVBR. Set the encoder to `qaac`, `aac_at`, or `aac` to require
a specific backend. Use `QAAC_COMMAND` or `FFMPEG_COMMAND` when an executable is
not on `PATH`.

`LOCAL_IMPORT_TRANSCODE_TIMEOUT` limits each encoder/remux operation and defaults
to 1800 seconds.

## Editing recent uploads

Send `/edit` after uploading to choose from recent tracks and open an inline edit menu. You can change artist, title, album, genre, or cover; artist/title changes also move the file to the matching `{Artist}/{Title}.ext` path. Choose `Cover` and send a JPEG/PNG image, image URL, or YouTube/niconico/bilibili URL to embed it as the front cover. Choose `Lyrics` to search QQ Music / Kugou / Netease again and replace the embedded lyrics, or adjust the embedded LRC `[offset:+/-ms]` tag with quick offsets or a custom seconds value. Positive offsets make lyrics appear earlier; negative offsets make them appear later.

Use `/edit song name` to search the whole `MUSIC_FOLDER` by filename, path, and metadata tags, then choose a matching track to edit.

## Large file support (Pyrogram)

Telegram Bot API limits single file downloads to 20MB. With Pyrogram (MTProto protocol) you can download up to 2GB.

1. Go to [my.telegram.org](https://my.telegram.org/apps) and create an app to get `api_id` and `api_hash`
2. Add them to `.env`:

```ini
API_ID=your_api_id
API_HASH=your_api_hash
```

Without these, files >20MB will be rejected with a prompt.

Pyrogram can optionally use `TgCrypto` to accelerate MTProto encryption, but it
is not required for large-file downloads and is not installed by default.
Current TgCrypto releases do not provide Windows wheels for recent CPython
versions, so adding it may require Microsoft C++ Build Tools.

## Post-processing hook

After each successful upload, the bot can run a custom command. Set `POST_HOOK` in `.env`:

```ini
POST_HOOK=python /path/to/script.py {path}
```

The hook is parsed into command arguments and is not run through a shell. The `{path}` placeholder is replaced with the saved file path as part of a single argument, and the same path is also available in the `NAVIDROME_UPLOADER_PATH` environment variable. Examples:

- Regenerate a playlist after upload
- Trigger external sync scripts
- Send notifications

The hook runs in the background with a 60-second timeout. Non-zero exits and stderr/stdout are logged.

## License

MIT
