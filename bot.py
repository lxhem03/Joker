# bot.py (updated with fixes and fancy progress)

import os
import asyncio
import logging
import time
import random
import subprocess
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import libtorrent as lt
import aiohttp
from config import API_ID, API_HASH, BOT_TOKEN, DOWNLOAD_DIR

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Client("mirror_leech_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Helper: Get video duration
def get_video_duration(file_path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        return float(result.stdout)
    except Exception:
        return None

# Helper: Generate random thumbnail
def generate_thumbnail(file_path):
    duration = get_video_duration(file_path)
    if duration is None:
        return None
    random_time = random.uniform(10, duration - 10) if duration > 20 else duration / 2
    thumb_path = os.path.join(DOWNLOAD_DIR, "thumb.jpg")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", file_path, "-ss", str(random_time), "-vframes", "1",
             "-vf", "scale=320:-1", thumb_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return thumb_path if os.path.exists(thumb_path) else None
    except Exception:
        return None

# Fancy progress bar (20 blocks, each 5%)
def get_progress_bar(percentage: int) -> str:
    filled = percentage // 5
    return "▣" * filled + "▢" * (20 - filled)

# Format size in MB
def format_size(bytes_size: int) -> str:
    return f"{bytes_size / (1024 * 1024):.2f} MB"

# Format time
def format_time(seconds: int) -> str:
    mins, secs = divmod(seconds, 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        return f"{hrs}h {mins}m {secs}s"
    elif mins:
        return f"{mins}m {secs}s"
    else:
        return f"{secs}s"

# Upload progress callback (updates every ~7s to avoid MESSAGE_NOT_MODIFIED)
async def upload_progress(current, total, status_msg, start_time, file_name):
    if not hasattr(upload_progress, "last_update"):
        upload_progress.last_update = 0
    now = time.time()
    if now - upload_progress.last_update < 7:
        return
    upload_progress.last_update = now

    percentage = int(current / total * 100)
    elapsed = int(now - start_time)
    speed = current / elapsed if elapsed > 0 else 0

    text = f"🔹 <b>{file_name}</b>\n{get_progress_bar(percentage)}\n\n" \
           f"🔗 Size: {format_size(current)} / {format_size(total)}\n" \
           f"️⏳ Done: {percentage}%\n" \
           f"🚀 Speed: {format_size(speed)}/s\n" \
           f"⏰ Elapsed: {format_time(elapsed)}"

    try:
        await status_msg.edit_text(text)
    except Exception:
        pass

# Command: /start
@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    await message.reply("Welcome to Mirror Leech Bot!\n\n"
                        "Use /leech <url> for direct links\n"
                        "/qbit <magnet or .torrent url> for torrents")

# /mirror - Direct download
@app.on_message(filters.command("leech") & filters.private)
async def mirror_direct(client, message):
    if len(message.command) < 2:
        return await message.reply("Usage: /leech <url>")
    
    url = message.command[1]
    file_name = url.split('/')[-1] or "downloaded_file"
    file_path = os.path.join(DOWNLOAD_DIR, file_name)
    
    status_msg = await message.reply("🔹 Starting download...")
    start_time = time.time()
    last_update = 0

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise Exception("Download failed")
                total_size = int(resp.headers.get('Content-Length', 0))
                downloaded = 0
                with open(file_path, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()
                            if now - last_update >= 7:
                                last_update = now
                                elapsed = int(now - start_time)
                                speed = downloaded / elapsed if elapsed > 0 else 0
                                percentage = int(downloaded / total_size * 100) if total_size else 0
                                
                                text = f"🔹 <b>{file_name}</b>\n{get_progress_bar(percentage)}\n\n" \
                                       f"🔗 Size: {format_size(downloaded)} / {format_size(total_size)}\n" \
                                       f"⏳ Done: {percentage}%\n" \
                                       f"🚀 Speed: {format_size(speed)}/s\n" \
                                       f"⏰ Elapsed: {format_time(elapsed)}"
                                
                                try:
                                    await status_msg.edit_text(text)
                                except Exception:
                                    pass

        await status_msg.edit_text("✅ Download complete! Preparing upload...")

        thumb = generate_thumbnail(file_path) if file_path.lower().endswith(('.mp4', '.mkv', '.avi', '.webm')) else None
        duration = get_video_duration(file_path)
        caption = f"Source: {url}"
        if duration:
            caption += f"\nDuration: {format_time(int(duration))}"

        await message.reply_document(
            file_path,
            caption=caption,
            thumb=thumb,
            progress=upload_progress,
            progress_args=(status_msg, start_time, file_name)
        )

        os.remove(file_path)
        if thumb:
            os.remove(thumb)
        await status_msg.edit_text("✅ All done!")
    except Exception as e:
        logger.error(e)
        await status_msg.edit_text(f"❌ Error: {str(e)}")

# /leech - Torrent/Magnet
@app.on_message(filters.command("qbit") & filters.private)
async def leech_torrent(client, message):
    if len(message.command) < 2:
        return await message.reply("Usage: /qbit <magnet or .torrent url>")
    
    torrent_link = message.command[1]
    status_msg = await message.reply("🔹 Adding torrent...")
    start_time = time.time()
    last_update = 0

    try:
        ses = lt.session()
        ses.listen_on(6881, 6891)

        params = {
            'save_path': DOWNLOAD_DIR,
            'storage_mode': lt.storage_mode_t.storage_mode_sparse,
        }

        if torrent_link.startswith("magnet:"):
            handle = lt.add_magnet_uri(ses, torrent_link, params)
        else:
            # Download .torrent file first
            torrent_file = os.path.join(DOWNLOAD_DIR, "temp.torrent")
            async with aiohttp.ClientSession() as session:
                async with session.get(torrent_link) as resp:
                    with open(torrent_file, 'wb') as f:
                        f.write(await resp.read())
            handle = ses.add_torrent({'ti': lt.torrent_info(torrent_file), **params})
            os.remove(torrent_file)

        # Wait for metadata
        while not handle.has_metadata():
            await asyncio.sleep(1)

        ti = handle.get_torrent_info()
        torrent_name = ti.name() if ti.num_files() > 1 else os.path.basename(ti.files().file_path(0))

        await status_msg.edit_text(f"🔹 <b>{torrent_name}</b>\nDownloading...")

        while not handle.status().is_seeding:
            s = handle.status()
            progress = int(s.progress * 100)
            now = time.time()
            if now - last_update >= 7:
                last_update = now
                elapsed = int(now - start_time)
                dl_speed = s.download_rate
                ul_speed = s.upload_rate
                seeders = s.num_seeds
                leechers = s.num_peers - s.num_seeds

                text = f"🔹 <b>{torrent_name}</b>\n{get_progress_bar(progress)}\n\n" \
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

        await status_msg.edit_text("✅ Torrent complete! Uploading files...")

        # Get list of files
        files = []
        fs = ti.files()
        for i in range(fs.num_files()):
            rel_path = fs.file_path(i)
            full_path = os.path.join(DOWNLOAD_DIR, rel_path)
            if os.path.exists(full_path):
                files.append((rel_path, full_path))

        for rel_path, full_path in files:
            thumb = generate_thumbnail(full_path) if full_path.lower().endswith(('.mp4', '.mkv', '.avi', '.webm')) else None
            duration = get_video_duration(full_path)
            caption = f"Source: {torrent_link}"
            if duration:
                caption += f"\nDuration: {format_time(int(duration))}"

            await message.reply_document(
                full_path,
                caption=caption,
                thumb=thumb,
                progress=upload_progress,
                progress_args=(status_msg, start_time, rel_path)
            )
            if thumb:
                os.remove(thumb)
        ses.remove_torrent(handle)
        await status_msg.edit_text("✅ All done!")
        os.remove(full_path)    
    except Exception as e:
        logger.error(e)
        await status_msg.edit_text(f"❌ Error: {str(e)}")

app.run()
