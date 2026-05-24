"""
lyric-filler: Scan music files for missing synced lyrics, search from
QQ Music / Kugou / Netease, interactively pick and embed into tags.
"""
import sys
import re
import json
import base64
import urllib.parse
from pathlib import Path
from typing import Optional

import requests
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, USLT
from mutagen.mp4 import MP4
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

EXTENSIONS = {'.mp3', '.flac', '.m4a', '.ogg', '.wma'}

# ─── Tag helpers ──────────────────────────────────────────────────────────────

def get_meta(filepath: Path) -> dict:
    """Extract artist, title, duration from audio file."""
    audio = MutagenFile(str(filepath))
    if audio is None:
        return {}
    meta = {'artist': '', 'title': '', 'duration': 0}
    if audio.info:
        meta['duration'] = int(getattr(audio.info, 'length', 0) * 1000)

    tags = audio.tags
    if tags is None:
        return meta

    # ID3
    for key in ('TPE1', 'TPE2'):
        if key in tags:
            meta['artist'] = str(tags[key])
            break
    if 'TIT2' in tags:
        meta['title'] = str(tags['TIT2'])

    # Vorbis / FLAC
    if hasattr(tags, 'get'):
        if not meta['artist']:
            meta['artist'] = (tags.get('artist') or tags.get('ARTIST') or [''])[0]
        if not meta['title']:
            meta['title'] = (tags.get('title') or tags.get('TITLE') or [''])[0]

    # MP4
    if isinstance(audio, MP4):
        meta['artist'] = (audio.tags.get('\xa9ART') or [''])[0]
        meta['title'] = (audio.tags.get('\xa9nam') or [''])[0]

    return meta


def has_synced_lyrics(filepath: Path) -> bool:
    """Check if file already has synced (timestamped) lyrics."""
    audio = MutagenFile(str(filepath))
    if audio is None or audio.tags is None:
        return False
    tags = audio.tags

    # ID3: check USLT and SYLT
    for key in tags.keys():
        if key.startswith('USLT'):
            text = str(tags[key])
            if re.search(r'\[\d{1,2}:\d{2}\.\d{2,3}\]', text):
                return True
        if key.startswith('SYLT'):
            return True

    # Vorbis/FLAC
    if hasattr(tags, 'get'):
        for field in ('LYRICS', 'UNSYNCEDLYRICS', 'lyrics', 'SYNCEDLYRICS'):
            vals = tags.get(field)
            if vals:
                for v in vals:
                    if re.search(r'\[\d{1,2}:\d{2}\.\d{2,3}\]', v):
                        return True

    # MP4
    if isinstance(audio, MP4):
        lyr = audio.tags.get('\xa9lyr')
        if lyr:
            for v in lyr:
                if re.search(r'\[\d{1,2}:\d{2}\.\d{2,3}\]', v):
                    return True
    return False


def write_lyrics(filepath: Path, lrc_text: str):
    """Write synced lyrics into the appropriate tag field."""
    audio = MutagenFile(str(filepath))
    if audio is None:
        return

    ext = filepath.suffix.lower()
    if ext == '.mp3':
        tags = audio.tags or ID3()
        # Remove existing USLT
        tags.delall('USLT')
        tags.add(USLT(encoding=3, lang='XXX', desc='', text=lrc_text))
        tags.save(str(filepath))
    elif ext in ('.flac', '.ogg'):
        audio.tags['LYRICS'] = [lrc_text]
        audio.save()
    elif ext == '.m4a':
        audio.tags['\xa9lyr'] = [lrc_text]
        audio.save()
    else:
        # Fallback: try as ID3
        try:
            tags = ID3(str(filepath))
            tags.delall('USLT')
            tags.add(USLT(encoding=3, lang='XXX', desc='', text=lrc_text))
            tags.save()
        except Exception:
            print(f"  [!] Unsupported format: {ext}")


# ─── Netease source ──────────────────────────────────────────────────────────

NETEASE_KEY = b'rFgB&h#%2?^eDg:Q'
NETEASE_TOKEN = "bf8bfeabb1aa84f9c8c3906c04a04fb864322804c83f5d607e91a04eae463c9436bd1a17ec353cf780b396507a3f7464e8a60f4bbc019437993166e004087dd32d1490298caf655c2353e58daa0bc13cc7d5c198250968580b12c1b8817e3f5c807e650dd04abd3fb8130b7ae43fcc5b"


def _netease_encrypt(data: dict) -> str:
    text = json.dumps(data).encode('utf-8')
    # Pad PKCS7
    pad_len = 16 - len(text) % 16
    text += bytes([pad_len]) * pad_len
    cipher = Cipher(algorithms.AES(NETEASE_KEY), modes.ECB())
    enc = cipher.encryptor()
    ct = enc.update(text) + enc.finalize()
    return ct.hex().upper()


def netease_search(title: str, artist: str) -> list:
    payload = {
        'method': 'POST',
        'url': 'https://music.163.com/api/search/get',
        'params': {'s': f'{title} {artist}', 'type': 1, 'limit': 10, 'offset': 0}
    }
    eparams = _netease_encrypt(payload)
    resp = requests.post(
        'https://music.163.com/api/linux/forward',
        data=f'eparams={eparams}',
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': 'https://music.163.com',
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.90 Safari/537.36',
            'Cookie': f'MUSIC_A={NETEASE_TOKEN}'
        },
        timeout=10
    )
    results = []
    try:
        data = resp.json()
        for song in data.get('result', {}).get('songs', []):
            artists = [a['name'] for a in song.get('artists', []) if 'name' in a]
            results.append({
                'id': song['id'],
                'title': song.get('name', ''),
                'artist': ', '.join(artists),
                'album': song.get('album', {}).get('name', ''),
            })
    except Exception:
        pass
    return results


def netease_get_lyric(song_id: int) -> Optional[str]:
    payload = {
        'method': 'POST',
        'url': 'https://music.163.com/api/song/lyric?lv=-1&kv=-1&tv=-1',
        'params': {'id': song_id}
    }
    eparams = _netease_encrypt(payload)
    resp = requests.post(
        'https://music.163.com/api/linux/forward',
        data=f'eparams={eparams}',
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': 'https://music.163.com',
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64)',
            'Cookie': f'MUSIC_A={NETEASE_TOKEN}'
        },
        timeout=10
    )
    try:
        data = resp.json()
        lrc = data.get('lrc', {}).get('lyric', '')
        tlyric = data.get('tlyric', {}).get('lyric', '')
        if lrc and re.search(r'\[\d{1,2}:\d{2}\.\d{2,3}\]', lrc):
            if tlyric:
                lrc = merge_translation(lrc, tlyric)
            return lrc
    except Exception:
        pass
    return None


# ─── Kugou source ────────────────────────────────────────────────────────────

KRC_KEY = bytes([64, 71, 97, 119, 94, 50, 116, 71, 81, 54, 49, 45, 206, 210, 110, 105])


def _decrypt_krc(data: bytes) -> Optional[str]:
    """Decrypt KRC format lyrics."""
    if len(data) < 4 or data[:4] != b'krc1':
        return None
    encrypted = data[4:]
    decrypted = bytearray(len(encrypted))
    for i in range(len(encrypted)):
        decrypted[i] = encrypted[i] ^ KRC_KEY[i % len(KRC_KEY)]
    try:
        import zlib
        decompressed = zlib.decompress(bytes(decrypted))
        return decompressed.decode('utf-8', errors='replace')
    except Exception:
        return None


def _krc_to_lrc(krc_text: str) -> str:
    """Convert KRC format to word-level synced LRC."""
    lines = []
    for line in krc_text.split('\n'):
        line = line.strip()
        m = re.match(r'\[(\d+),(\d+)\](.*)', line)
        if m:
            line_ms = int(m.group(1))
            body = m.group(3)
            # Line-level timestamp
            mm = line_ms // 60000
            ss = (line_ms % 60000) // 1000
            cs = (line_ms % 1000) // 10
            # Convert word-level <offset,dur,0>word to [mm:ss.cs]word
            words = re.findall(r'<(\d+),(\d+),\d+>([^<]*)', body)
            if words:
                parts = []
                for offset, dur, word in words:
                    abs_ms = line_ms + int(offset)
                    w_mm = abs_ms // 60000
                    w_ss = (abs_ms % 60000) // 1000
                    w_cs = (abs_ms % 1000) // 10
                    parts.append(f'[{w_mm:02d}:{w_ss:02d}.{w_cs:02d}]{word}')
                lines.append(f'[{mm:02d}:{ss:02d}.{cs:02d}]' + ''.join(parts))
            else:
                text = re.sub(r'<\d+,\d+,\d+>', '', body)
                lines.append(f'[{mm:02d}:{ss:02d}.{cs:02d}]{text}')
        elif line.startswith('['):
            lines.append(line)
    return '\n'.join(lines)


def kugou_search(title: str, artist: str, duration_ms: int) -> list:
    url = (
        f"http://lyrics.kugou.com/search?ver=1&man=yes&client=pc"
        f"&keyword={urllib.parse.quote(artist + '-' + title)}"
        f"&duration={duration_ms}&hash="
    )
    results = []
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        for item in data.get('candidates', []):
            if item.get('id') and item.get('accesskey'):
                results.append({
                    'id': item['id'],
                    'key': item['accesskey'],
                    'title': item.get('song', ''),
                    'artist': item.get('singer', ''),
                })
    except Exception:
        pass
    return results


def kugou_get_lyric(lid: str, accesskey: str) -> Optional[str]:
    url = (
        f"http://lyrics.kugou.com/download?ver=1&client=pc"
        f"&id={lid}&accesskey={accesskey}&fmt=krc&charset=utf8"
    )
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        content = data.get('content', '')
        if content:
            raw = base64.b64decode(content)
            krc_text = _decrypt_krc(raw)
            if krc_text:
                return _krc_to_lrc(krc_text)
    except Exception:
        pass
    return None


# ─── QQ Music source ─────────────────────────────────────────────────────────

def qq_search(title: str, artist: str) -> list:
    url = (
        f"https://c.y.qq.com/soso/fcgi-bin/client_search_cp?"
        f"format=json&n=10&p=0&w={urllib.parse.quote(title + ' ' + artist)}&cr=1&g_tk=5381"
    )
    results = []
    try:
        resp = requests.get(url, headers={'Referer': 'https://y.qq.com'}, timeout=10)
        data = resp.json()
        for song in data.get('data', {}).get('song', {}).get('list', []):
            artists = [s.get('name', '') for s in song.get('singer', [])]
            results.append({
                'id': song.get('songmid', ''),
                'title': song.get('songname', ''),
                'artist': ', '.join(artists),
                'album': song.get('albumname', ''),
            })
    except Exception:
        pass
    return results


def qq_get_lyric(songmid: str) -> Optional[str]:
    import time
    url = (
        f"https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?"
        f"songmid={songmid}&pcachetime={int(time.time()*1000)}"
        f"&g_tk=5381&loginUin=0&hostUin=0&inCharset=utf8"
        f"&outCharset=utf-8&notice=0&platform=yqq&needNewCode=1&format=json"
    )
    try:
        resp = requests.get(url, headers={'Referer': 'https://y.qq.com'}, timeout=10)
        data = resp.json()
        b64lyric = data.get('lyric', '')
        if b64lyric:
            lrc = base64.b64decode(b64lyric).decode('utf-8', errors='replace')
            if re.search(r'\[\d{1,2}:\d{2}\.\d{2,3}\]', lrc):
                b64trans = data.get('trans', '')
                if b64trans:
                    tlrc = base64.b64decode(b64trans).decode('utf-8', errors='replace')
                    lrc = merge_translation(lrc, tlrc)
                return lrc
    except Exception:
        pass
    return None


# ─── Translation merge ────────────────────────────────────────────────────────

def merge_translation(orig: str, trans: str) -> str:
    """Merge translation lines: translation gets next orig timestamp - 1ms."""
    time_re = re.compile(r'^\[(\d+):(\d+)\.(\d+)\](.*)')

    trans_map = {}
    for line in trans.split('\n'):
        m = time_re.match(line.strip())
        if m:
            key = f'{m.group(1)}:{m.group(2)}.{m.group(3)}'
            text = m.group(4).strip()
            if text:
                trans_map[key] = text

    entries = []
    for line in orig.split('\n'):
        line = line.strip()
        if not line:
            continue
        m = time_re.match(line)
        if m:
            key = f'{m.group(1)}:{m.group(2)}.{m.group(3)}'
            cs_len = len(m.group(3))
            ms = (int(m.group(1)) * 60 + int(m.group(2))) * 1000
            frac = m.group(3)
            if len(frac) == 2:
                ms += int(frac) * 10
            else:
                ms += int(frac[:3])
            entries.append({'line': line, 'key': key, 'ms': ms, 'cs_len': cs_len,
                            'trans': trans_map.get(key)})
        else:
            entries.append({'line': line, 'key': None, 'ms': -1, 'cs_len': 2, 'trans': None})

    result = []
    for i, e in enumerate(entries):
        result.append(e['line'])
        if e['trans']:
            # Find next timed entry
            next_ms = -1
            next_cs_len = e['cs_len']
            for j in range(i + 1, len(entries)):
                if entries[j]['ms'] >= 0:
                    next_ms = entries[j]['ms']
                    next_cs_len = entries[j]['cs_len']
                    break

            if next_ms > 0:
                t_ms = next_ms - 1
                use_cs = next_cs_len
            else:
                t_ms = e['ms'] + 1000
                use_cs = e['cs_len']

            mm = t_ms // 60000
            ss = (t_ms % 60000) // 1000
            frac = t_ms % 1000
            if use_cs >= 3:
                ts = f'{mm:02d}:{ss:02d}.{frac:03d}'
            else:
                ts = f'{mm:02d}:{ss:02d}.{frac // 10:02d}'
            result.append(f'[{ts}]「{e["trans"]}」')

    return '\n'.join(result)


# ─── Interactive CLI ──────────────────────────────────────────────────────────

def preview_lrc(lrc: str, lines: int = 6) -> str:
    """Show first N timestamped lines."""
    out = []
    for line in lrc.split('\n'):
        if re.match(r'\[\d{1,2}:\d{2}', line.strip()):
            out.append(line.strip())
            if len(out) >= lines:
                break
    return '\n'.join(out)


def search_all_sources(title: str, artist: str, duration_ms: int) -> list:
    """Search all three sources, return list of (source, candidate, lyric_fetcher)."""
    candidates = []

    # Netease
    try:
        for c in netease_search(title, artist)[:5]:
            candidates.append(('netease', c, lambda cid=c['id']: netease_get_lyric(cid)))
    except Exception:
        pass

    # Kugou
    try:
        for c in kugou_search(title, artist, duration_ms)[:5]:
            candidates.append(('kugou', c, lambda lid=c['id'], k=c['key']: kugou_get_lyric(lid, k)))
    except Exception:
        pass

    # QQ
    try:
        for c in qq_search(title, artist)[:5]:
            candidates.append(('qq', c, lambda mid=c['id']: qq_get_lyric(mid)))
    except Exception:
        pass

    return candidates


def process_file(filepath: Path):
    meta = get_meta(filepath)
    if not meta.get('title'):
        print(f"  [skip] No title tag")
        return

    title = meta['title']
    artist = meta['artist']
    duration = meta.get('duration', 0)

    print(f"\n{'='*60}")
    print(f"  {artist} - {title}")
    print(f"  {filepath}")
    print(f"{'='*60}")

    candidates = search_all_sources(title, artist, duration)
    if not candidates:
        print("  No results found from any source.")
        return

    # Fetch lyrics and filter to those with synced content
    results = []
    for source, info, fetcher in candidates:
        try:
            lrc = fetcher()
            if lrc and re.search(r'\[\d{1,2}:\d{2}\.\d{2,3}\]', lrc):
                results.append((source, info, lrc))
        except Exception:
            pass

    if not results:
        print("  No synced lyrics found.")
        choice = input("  (i)nstrumental, (s)kip, (q)uit: ").strip().lower()
        if choice == 'i':
            write_lyrics(filepath, '[00:00.00]♪ Instrumental ♪')
            print(f"  ✓ Marked as instrumental")
        elif choice == 'q':
            sys.exit(0)
        return

    # Deduplicate by first 200 chars of lyrics
    seen = set()
    unique = []
    for source, info, lrc in results:
        key = lrc[:200]
        if key not in seen:
            seen.add(key)
            unique.append((source, info, lrc))
    results = unique

    # Display options
    for i, (source, info, lrc) in enumerate(results):
        print(f"\n  [{i+1}] ({source}) {info.get('artist','')} - {info.get('title','')}")
        print(f"      {info.get('album','')}")
        for pline in preview_lrc(lrc).split('\n'):
            print(f"      {pline}")

    # Prompt
    while True:
        choice = input(f"\n  Pick [1-{len(results)}], (s)kip, (i)nstrumental, (q)uit: ").strip().lower()
        if choice == 's':
            return
        if choice == 'q':
            sys.exit(0)
        if choice == 'i':
            write_lyrics(filepath, '[00:00.00]♪ Instrumental ♪')
            print(f"  ✓ Marked as instrumental (will be skipped next time)")
            return
        if choice == 'p':
            # Full preview
            idx = input("  Which # to preview fully? ").strip()
            if idx.isdigit() and 1 <= int(idx) <= len(results):
                print(results[int(idx)-1][2])
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(results):
            _, _, lrc = results[int(choice) - 1]
            write_lyrics(filepath, lrc)
            print(f"  ✓ Lyrics written!")
            return
        print("  Invalid input.")


def scan_and_process(directory: str):
    root = Path(directory)
    files = sorted(f for f in root.rglob('*') if f.suffix.lower() in EXTENSIONS)
    missing = [f for f in files if not has_synced_lyrics(f)]

    print(f"Scanned {len(files)} files, {len(missing)} missing synced lyrics.\n")

    for i, f in enumerate(missing):
        print(f"\n[{i+1}/{len(missing)}]", end='')
        process_file(f)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <music_directory>")
        sys.exit(1)
    scan_and_process(sys.argv[1])
