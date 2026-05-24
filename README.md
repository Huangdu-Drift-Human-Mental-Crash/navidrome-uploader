# Navidrome Uploader Bot

Upload audio files to [Navidrome](https://www.navidrome.org/) via Telegram, with automatic metadata parsing, synced lyrics search, and library scanning.

## Features

- Receives MP3 / FLAC / M4A / OGG / WAV etc. via Telegram
- Extracts ID3 / Vorbis / MP4 tags, organizes as `{Artist}/{Title}`
- Triggers Navidrome library scan via Subsonic API
- Searches QQ Music / Kugou / Netease for synced lyrics and embeds them
- `/edit` interactively edits recent uploads' artist/title/album and can replace lyrics

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
```

## Running

```bash
python bot.py                         # Linux/macOS
.venv\Scripts\python.exe bot.py     # Windows
```

## Directory structure

Files are stored as `{MUSIC_FOLDER}/{Artist}/{Title}.ext` (flat, no album subdirectories).

## Editing recent uploads

Send `/edit` after uploading to choose from recent tracks and open an inline edit menu. You can change artist, title, or album; artist/title changes also move the file to the matching `{Artist}/{Title}.ext` path. Choose `Lyrics` to search QQ Music / Kugou / Netease again and replace the embedded lyrics with one of the new results.

## Large file support (Pyrogram)

Telegram Bot API limits single file downloads to 20MB. With Pyrogram (MTProto protocol) you can download up to 2GB.

1. Go to [my.telegram.org](https://my.telegram.org/apps) and create an app to get `api_id` and `api_hash`
2. Add them to `.env`:

```ini
API_ID=your_api_id
API_HASH=your_api_hash
```

Without these, files >20MB will be rejected with a prompt.

## Post-processing hook

After each successful upload, the bot can run a custom command. Set `POST_HOOK` in `.env`:

```ini
POST_HOOK=python /path/to/script.py parms
```

The `{path}` placeholder is replaced with the saved file path. Examples:

- Regenerate a playlist after upload
- Trigger external sync scripts
- Send notifications

The hook runs with a 60-second timeout and won't block the bot on failure.

## License

MIT
