import os
import asyncio
import logging
import random
import subprocess
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import libtorrent as lt
import aiohttp
from config import API_ID, API_HASH, BOT_TOKEN, DOWNLOAD_DIR

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Client("mirror_leech_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

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

# Helper: Generate random thumbnail from video
def generate_thumbnail(file_path):
    duration = get_video_duration(file_path)
    if duration is None:
        return None
    random_time = random.uniform(0, duration)
    thumb_path = os.path.join(DOWNLOAD_DIR, "thumb.jpg")
    try:
        subprocess.run(
            ["ffmpeg", "-i", file_path, "-ss", str(random_time), "-vframes", "1",
             "-vf", "scale=320:-1", thumb_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        return thumb_path if os.path.exists(thumb_path) else None
    except Exception:
        return None

# Progress callback for uploads
async def upload_progress(current, total, status_msg):
    progress = int((current / total) * 100)
    try:
        await status_msg.edit_text(f"Uploading: {progress}%")
    except Exception:
        pass

# Command: /start
@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    await message.reply("Welcome to Mirror Leech Bot! Send /leech <url> for direct links or /qbit <magnet/torrent_url> for torrents.")

# Command: /mirror <direct_url> - For HTTP/HTTPS downloads
@app.on_message(filters.command("leech") & filters.private)
async def mirror_direct(client, message):
    if len(message.command) < 2:
        return await message.reply("Usage: /mirror <url>")
    
    url = message.command[1]
    status_msg = await message.reply("Starting download...")
    
    try:
        file_name = url.split('/')[-1] or "downloaded_file"
        file_path = os.path.join(DOWNLOAD_DIR, file_name)
        
        # Async download with progress
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise Exception("Failed to download")
                total_size = int(resp.headers.get('Content-Length', 0))
                downloaded = 0
                with open(file_path, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            progress = int((downloaded / total_size) * 100) if total_size else 0
                            await status_msg.edit_text(f"Downloading: {progress}% | Speed: {len(chunk)/1024:.2f} KB/s")
                            await asyncio.sleep(1)  # Throttle updates
        
        await status_msg.edit_text("Download complete. Preparing upload...")
        
        # Check if video and generate thumb + duration
        thumb = generate_thumbnail(file_path) if file_path.lower().endswith(('.mp4', '.mkv', '.avi')) else None
        duration = get_video_duration(file_path)
        
        # Upload with progress and caption
        caption = f"Source: {url}\nDuration: {duration:.2f} seconds" if duration else f"Source: {url}"
        await message.reply_document(
            file_path,
            caption=caption,
            thumb=thumb,
            progress=upload_progress,
            progress_args=(status_msg,)
        )
        
        # Cleanup
        os.remove(file_path)
        if thumb:
            os.remove(thumb)
        await status_msg.edit_text("Done!")
    except Exception as e:
        logger.error(e)
        await status_msg.edit_text(f"Error: {str(e)}")

# Command: /leech <magnet or torrent_url> - For torrents
@app.on_message(filters.command("qbit") & filters.private)
async def leech_torrent(client, message):
    if len(message.command) < 2:
        return await message.reply("Usage: /leech <magnet/torrent_url>")
    
    torrent_link = message.command[1]
    status_msg = await message.reply("Starting leech...")
    
    try:
        ses = lt.session()
        ses.listen_on(6881, 6891)
        
        if torrent_link.startswith("magnet:"):
            params = lt.parse_magnet_uri(torrent_link)
        else:
            # Assume it's a .torrent file URL; download it first
            torrent_file = os.path.join(DOWNLOAD_DIR, "temp.torrent")
            async with aiohttp.ClientSession() as session:
                async with session.get(torrent_link) as resp:
                    with open(torrent_file, 'wb') as f:
                        f.write(await resp.read())
            e = lt.bdecode(open(torrent_file, 'rb').read())
            params = lt.add_torrent_params()
            params.ti = lt.torrent_info(e)
            os.remove(torrent_file)
        
        params.save_path = DOWNLOAD_DIR
        handle = ses.add_torrent(params)
        
        # Progress tracking loop
        while not handle.status().is_seeding:
            s = handle.status()
            progress = int(s.progress * 100)
            dl_speed = s.download_rate / 1024 / 1024  # MB/s
            ul_speed = s.upload_rate / 1024 / 1024  # MB/s
            seeders = s.num_seeds
            leechers = s.num_peers - s.num_seeds
            state_str = ['queued', 'checking', 'downloading metadata', 'downloading', 'finished', 'seeding', 'allocating', 'checking fastresume']
            status_text = f"State: {state_str[s.state]}\nProgress: {progress}%\nDL Speed: {dl_speed:.2f} MB/s\nUL Speed: {ul_speed:.2f} MB/s\nSeeders: {seeders}\nLeechers: {leechers}"
            await status_msg.edit_text(status_text)
            await asyncio.sleep(5)
        
        await status_msg.edit_text("Download complete. Preparing upload...")
        
        # Find downloaded files (handle multiple)
        ti = handle.torrent_file()
        files = [os.path.join(DOWNLOAD_DIR, ti.file_path(i)) for i in range(ti.num_files())]
        for file_path in files:
            # Check if video and generate thumb + duration
            thumb = generate_thumbnail(file_path) if file_path.lower().endswith(('.mp4', '.mkv', '.avi')) else None
            duration = get_video_duration(file_path)
            
            # Upload with progress and caption
            caption = f"Source: {torrent_link}\nDuration: {duration:.2f} seconds" if duration else f"Source: {torrent_link}"
            await message.reply_document(
                file_path,
                caption=caption,
                thumb=thumb,
                progress=upload_progress,
                progress_args=(status_msg,)
            )
            
            # Cleanup
            if thumb:
                os.remove(thumb)
        
        # Remove torrent files
        ses.remove_torrent(handle)
        for file_path in files:
            if os.path.exists(file_path):
                os.remove(file_path)
        
        await status_msg.edit_text("Done!")
    except Exception as e:
        logger.error(e)
        await status_msg.edit_text(f"Error: {str(e)}")

@app.on_callback_query(filters.regex("cancel"))
async def cancel_download(client, callback: CallbackQuery):
    # Implement cancel logic here (e.g., stop session)
    await callback.answer("Download cancelled.")

app.run()
