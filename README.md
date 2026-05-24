# Navidrome Uploader Bot

Upload audio files to [Navidrome](https://www.navidrome.org/) via Telegram, with automatic metadata parsing, synced lyrics search, and library scanning.

## Features

- Receives MP3 / FLAC / M4A / OGG / WAV etc. via Telegram
- Extracts ID3 / Vorbis / MP4 tags, organizes as `{Artist}/{Title}`
- Triggers Navidrome library scan via Subsonic API
- Searches QQ Music / Kugou / Netease for synced lyrics and embeds them

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

## Large file support (Pyrogram)

Telegram Bot API limits single file downloads to 20MB. With Pyrogram (MTProto protocol) you can download up to 2GB.

1. Go to [my.telegram.org](https://my.telegram.org/apps) and create an app to get `api_id` and `api_hash`
2. Add them to `.env`:

```ini
API_ID=your_api_id
API_HASH=your_api_hash
```

Without these, files >20MB will be rejected with a prompt.

## License

MIT
