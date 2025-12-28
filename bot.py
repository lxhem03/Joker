# bot.py (optimized for concurrency, fixed delays, multi-file uploads, task IDs)

import os
import asyncio
import logging
import time
import random
import re
import json
import mimetypes
from urllib.parse import urlparse, unquote
import requests
import aiohttp
from pyrogram import Client, filters
import libtorrent as lt
from config import API_ID, API_HASH, BOT_TOKEN, DOWNLOAD_DIR
import uuid

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Client("mirror_leech_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Global libtorrent session for efficiency
lt_session = lt.session()
lt_session.listen_on(6881, 6891)

# Active tasks dict {task_id: {'user_id': int, 'status_msg': Message, 'handle': lt_handle or None, 'start_time': float, 'file_paths': list, 'is_cancelled': bool}}
active_tasks = {}

# --- Filename Detection (unchanged) ---
def extract_filename_from_headers(headers):
    cd = headers.get("Content-Disposition", "")
    if not cd:
        return None
    match = re.search(r"filename\*=UTF-8''(.+)", cd)
    if match:
        return unquote(match.group(1))
    match = re.search(r'filename="?([^";]+)"?', cd)
    if match:
        return match.group(1)
    return None

def extract_filename_from_url(url):
    path = urlparse(url).path
    name = unquote(os.path.basename(path))
    return name if "." in name else None

def extract_from_html(text):
    match = re.search(r'property="og:title" content="([^"]+)"', text)
    if match:
        return match.group(1)
    match = re.search(r"<title>(.*?)</title>", text, re.I)
    if match:
        return match.group(1).strip()
    return None

def extract_from_json(text):
    try:
        data = json.loads(text)
        for key in ("filename", "file_name", "name", "title"):
            if key in data and isinstance(data[key], str):
                return data[key]
    except Exception:
        pass
    return None

def guess_from_content_type(headers):
    ct = headers.get("Content-Type")
    if not ct:
        return None
    ext = mimetypes.guess_extension(ct.split(";")[0].strip())
    if ext:
        return f"file{ext}"
    return None

def get_filename_from_ddl(url, timeout=15):
    try:
        r = requests.head(url, allow_redirects=True, headers=HEADERS, timeout=timeout)
        if r.status_code < 400:
            name = extract_filename_from_headers(r.headers)
            if name:
                return name
            name = extract_filename_from_url(r.url)
            if name:
                return name
    except Exception:
        pass

    name = extract_filename_from_url(url)
    if name:
        return name

    try:
        r = requests.get(url, headers={**HEADERS, "Range": "bytes=0-2048"}, timeout=timeout, stream=True)
        if r.status_code in (200, 206):
            name = extract_filename_from_headers(r.headers)
            if name:
                return name
            name = extract_from_json(r.text)
            if name:
                return name
            name = extract_from_html(r.text)
            if name:
                return name
            name = guess_from_content_type(r.headers)
            if name:
                return name
    except Exception:
        pass

    return "file.bin"

# --- Async FFmpeg helpers to avoid delays ---
async def async_get_video_duration(file_path):
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", file_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        return float(stdout.decode().strip())
    except Exception:
        return None

async def async_generate_thumbnail(file_path):
    duration = await async_get_video_duration(file_path)
    if duration is None:
        return None
    random_time = random.uniform(10, duration - 10) if duration > 20 else duration / 2
    thumb_path = os.path.join(DOWNLOAD_DIR, f"thumb_{uuid.uuid4().hex[:8]}.jpg")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", file_path, "-ss", str(random_time), "-vframes", "1",
            "-vf", "scale=320:-1", thumb_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        return thumb_path if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0 else None
    except Exception:
        return None

# --- Progress helpers (unchanged) ---
def get_progress_bar(percentage: int) -> str:
    filled = percentage // 5
    return "▣" * filled + "▢" * (20 - filled)

def format_size(bytes_size: int) -> str:
    return f"{bytes_size / (1024 * 1024):.2f} MB"

def format_time(seconds: int) -> str:
    mins, secs = divmod(seconds, 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        return f"{hrs}h {mins}m {secs}s"
    elif mins:
        return f"{mins}m {secs}s"
    else:
        return f"{secs}s"

async def upload_progress(current, total, status_msg, start_time, file_name):
    if not hasattr(status_msg, "last_update"):
        status_msg.last_update = 0
    now = time.time()
    if now - status_msg.last_update < 7:
        return
    status_msg.last_update = now

    percentage = int(current / total * 100) if total else 0
    elapsed = int(now - start_time)
    speed = current / elapsed if elapsed > 0 else 0

    text = f"🔹 <b>{file_name}</b>\n{get_progress_bar(percentage)}\n\n" \
           f"🔗 Size: {format_size(current)} / {format_size(total)}\n" \
           f"⏳ Done: {percentage}%\n" \
           f"🚀 Speed: {format_size(speed)}/s\n" \
           f"⏰ Elapsed: {format_time(elapsed)}"

    try:
        await status_msg.edit_text(text)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(e)

# /start (unchanged)
@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    await message.reply("Welcome to Mirror Leech Bot!\n\n"
                        "• /mirror <url> - Direct download & upload\n"
                        "• /leech <magnet/.torrent> - Torrent leech")

# /mirror
@app.on_message(filters.command("leech") & filters.private)
async def mirror_direct(client, message):
    if len(message.command) < 2:
        return await message.reply("Usage: /leech <url>")

    user_id = message.from_user.id
    url = message.command[1].strip()
    task_id = str(uuid.uuid4().hex[:12])
    status_msg = await message.reply(f"Task ID: {task_id}\n🔍 Detecting filename...")
    active_tasks[task_id] = {'user_id': user_id, 'status_msg': status_msg, 'handle': None, 'start_time': time.time(), 'file_paths': [], 'is_cancelled': False}

    try:
        file_name = get_filename_from_ddl(url)
        file_path = os.path.join(DOWNLOAD_DIR, file_name)

        await status_msg.edit_text(f"Task ID: {task_id}\n🔹 Starting download...")
        start_time = active_tasks[task_id]['start_time']
        last_update = 0
        downloaded = 0

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status >= 400:
                    raise Exception(f"HTTP Error {resp.status}: {resp.reason}")
                
                total_size = int(resp.headers.get('Content-Length', 0))
                
                with open(file_path, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        if active_tasks[task_id]['is_cancelled']:
                            raise Exception("Task cancelled")
                        if chunk:
                            size = len(chunk)
                            f.write(chunk)
                            downloaded += size
                            now = time.time()
                            if now - last_update >= 7:
                                last_update = now
                                elapsed = int(now - start_time)
                                speed = downloaded / elapsed if elapsed > 0 else 0
                                percentage = int(downloaded / total_size * 100) if total_size else 0

                                text = f"Task ID: {task_id}\n🔹 <b>{file_name}</b>\n{get_progress_bar(percentage)}\n\n" \
                                       f"🔗 Size: {format_size(downloaded)} / {format_size(total_size or 0)}\n" \
                                       f"⏳ Done: {percentage}%\n" \
                                       f"🚀 Speed: {format_size(speed)}/s\n" \
                                       f"⏰ Elapsed: {format_time(elapsed)}"

                                try:
                                    await status_msg.edit_text(text)
                                except Exception:
                                    pass

        await status_msg.edit_text(f"Task ID: {task_id}\n✅ Download complete! Preparing upload...")

        active_tasks[task_id]['file_paths'] = [file_path]

        # Upload immediately
        await upload_files(task_id, message, url)  # Pass original url for source if needed, but caption is filename

    except aiohttp.ClientError as e:
        await status_msg.edit_text(f"Task ID: {task_id}\n❌ Network error: {str(e)}")
    except asyncio.TimeoutError:
        await status_msg.edit_text(f"Task ID: {task_id}\n❌ Download timed out")
    except Exception as e:
        logger.error(e)
        await status_msg.edit_text(f"Task ID: {task_id}\n❌ Error: {str(e)}")
    finally:
        active_tasks.pop(task_id, None)

# Helper to upload files (for both mirror and leech)
async def upload_files(task_id, message, source):
    if task_id not in active_tasks:
        return

    status_msg = active_tasks[task_id]['status_msg']
    start_time = active_tasks[task_id]['start_time']
    file_paths = active_tasks[task_id]['file_paths']

    await status_msg.edit_text(f"Task ID: {task_id}\n✅ Complete! Uploading files...")

    upload_tasks = []
    for file_path in file_paths:
        if active_tasks[task_id]['is_cancelled']:
            break
        rel_path = os.path.basename(file_path)
        upload_tasks.append(upload_single_file(message, file_path, rel_path, status_msg, start_time, source))

    await asyncio.gather(*upload_tasks, return_exceptions=True)

    await status_msg.edit_text(f"Task ID: {task_id}\n✅ All done!")
    # Cleanup files
    for file_path in file_paths:
        if os.path.exists(file_path):
            os.remove(file_path)

async def upload_single_file(message, file_path, rel_path, status_msg, start_time, source):
    is_video = file_path.lower().endswith(('.mp4', '.mkv', '.avi', '.webm'))
    thumb = await async_generate_thumbnail(file_path) if is_video else None
    duration = await async_get_video_duration(file_path) if is_video else None
    caption = rel_path  # Filename as caption
    if duration:
        caption += f"\nDuration: {format_time(int(duration))}"

    await message.reply_document(
        file_path,
        caption=caption,
        thumb=thumb,
        progress=upload_progress,
        progress_args=(status_msg, start_time, rel_path)
    )

    if thumb and os.path.exists(thumb):
        os.remove(thumb)

# /leech
@app.on_message(filters.command("qbit") & filters.private)
async def leech_torrent(client, message):
    if len(message.command) < 2:
        return await message.reply("Usage: /qbit <magnet or .torrent url>")

    user_id = message.from_user.id
    torrent_link = message.command[1]
    task_id = str(uuid.uuid4().hex[:12])
    status_msg = await message.reply(f"Task ID: {task_id}\n🔹 Adding torrent...")
    active_tasks[task_id] = {'user_id': user_id, 'status_msg': status_msg, 'handle': None, 'start_time': time.time(), 'file_paths': [], 'is_cancelled': False}

    try:
        params = {
            'save_path': DOWNLOAD_DIR,
            'storage_mode': lt.storage_mode_t.storage_mode_sparse,
        }

        if torrent_link.startswith("magnet:"):
            handle = lt.add_magnet_uri(lt_session, torrent_link, params)
        else:
            torrent_file = os.path.join(DOWNLOAD_DIR, "temp.torrent")
            async with aiohttp.ClientSession() as session:
                async with session.get(torrent_link) as resp:
                    if resp.status != 200:
                        raise Exception(f"Failed to fetch .torrent: HTTP {resp.status}")
                    with open(torrent_file, 'wb') as f:
                        f.write(await resp.read())
            handle = lt_session.add_torrent({'ti': lt.torrent_info(torrent_file), **params})
            os.remove(torrent_file)

        active_tasks[task_id]['handle'] = handle

        while not handle.has_metadata():
            if active_tasks[task_id]['is_cancelled']:
                raise Exception("Task cancelled")
            await asyncio.sleep(1)

        ti = handle.get_torrent_info()
        torrent_name = ti.name() if ti.num_files() > 1 else os.path.basename(ti.files().file_path(0))

        last_update = 0
        while not handle.status().is_seeding:
            if active_tasks[task_id]['is_cancelled']:
                raise Exception("Task cancelled")
            s = handle.status()
            progress = int(s.progress * 100)
            now = time.time()
            if now - last_update >= 7:
                last_update = now
                elapsed = int(now - active_tasks[task_id]['start_time'])
                dl_speed = s.download_rate
                ul_speed = s.upload_rate
                seeders = s.num_seeds
                leechers = s.num_peers - s.num_seeds

                text = f"Task ID: {task_id}\n🔹 <b>{torrent_name}</b>\n{get_progress_bar(progress)}\n\n" \
                       f"🔗 Size: {format_size(s.total_done)} / {format_size(s.total_wanted)}\n" \
                       f"⏳ Done: {progress}%\n" \
                       f"🚀 Speed: ↓ {format_size(dl_speed)}/s | ↑ {format_size(ul_speed)}/s\n" \
                       f"👥 Seeders: {seeders} | Leechers: {leechers}\n" \
                       f"⏰ Elapsed: {format_time(elapsed)}"

                try:
                    await status_msg.edit_text(text)
                except Exception:
                    pass
            await asyncio.sleep(1)

        # Collect files
        fs = ti.files()
        for i in range(fs.num_files()):
            rel_path = fs.file_path(i)
            full_path = os.path.join(DOWNLOAD_DIR, rel_path)
            if os.path.exists(full_path):
                active_tasks[task_id]['file_paths'].append(full_path)

        # Upload concurrently
        await upload_files(task_id, message, torrent_link)

    except Exception as e:
        logger.error(e)
        await status_msg.edit_text(f"Task ID: {task_id}\n❌ Error: {str(e)}")
    finally:
        if active_tasks[task_id]['handle']:
            lt_session.remove_torrent(active_tasks[task_id]['handle'])
        active_tasks.pop(task_id, None)

app.run()
