import os
import re
import sys
import asyncio
import logging
import tempfile
import shutil
import requests
import yt_dlp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "8500563038:AAG1VzK7nUW-KcrrCM5DhapxyazffMOoots")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

USER_DOWNLOAD_CACHE = {}

URL_REGEX = re.compile(
    r"(https?://)?(www\.|vm\.|vt\.|t\.)?(youtube\.com|youtu\.be|tiktok\.com)/[^\s]+"
)


async def animate_download(message):
    """Looping animated dots status while downloading."""
    frames = ["⏳ Downloading.", "⏳ Downloading..", "⏳ Downloading..."]
    idx = 0
    try:
        while True:
            await asyncio.sleep(0.8)
            idx = (idx + 1) % len(frames)
            try:
                await message.edit_text(frames[idx])
            except Exception:
                pass
    except asyncio.CancelledError:
        pass


def resolve_tiktok_url(raw_url):
    """Resolve TikTok short links (vt.tiktok.com) and detect photo posts."""
    if "tiktok.com" in raw_url.lower():
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            r = requests.head(raw_url, headers=headers, allow_redirects=True, timeout=5)
            final_url = r.url
            if not final_url or "tiktok.com" not in final_url:
                r = requests.get(raw_url, headers=headers, allow_redirects=True, timeout=5)
                final_url = r.url
        except Exception:
            final_url = raw_url

        is_photo = "/photo/" in final_url.lower()
        clean_url = final_url.replace("/photo/", "/video/").split("?")[0]
        return final_url, clean_url, is_photo
    
    return raw_url, raw_url, False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start message."""
    await update.message.reply_text("👋 Send a YouTube or TikTok link to download MP3, MP4, or Photo.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle YouTube or TikTok links."""
    text = update.message.text.strip() if update.message.text else ""
    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text("❌ Please send a valid YouTube or TikTok link.")
        return

    raw_url = match.group(0)
    loop = asyncio.get_event_loop()
    
    final_url, clean_url, is_photo_link = await loop.run_in_executor(None, lambda: resolve_tiktok_url(raw_url))

    if is_photo_link:
        keyboard = [
            [
                InlineKeyboardButton("🖼 Photo", callback_data="fmt_photo"),
                InlineKeyboardButton("🎵 MP3", callback_data="fmt_mp3"),
            ]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton("🎵 MP3", callback_data="fmt_mp3"),
                InlineKeyboardButton("🎬 MP4", callback_data="fmt_mp4"),
            ]
        ]

    user_id = update.effective_user.id
    reply_markup = InlineKeyboardMarkup(keyboard)

    prompt_msg = await update.message.reply_text(
        "Select format:",
        reply_markup=reply_markup,
    )

    USER_DOWNLOAD_CACHE[user_id] = {
        "url": clean_url,
        "raw_url": final_url,
        "is_photo_post": is_photo_link,
        "user_msg_id": update.message.message_id,
        "prompt_msg_id": prompt_msg.message_id,
    }


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process download & send media (Photos, MP3, or MP4)."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if user_id not in USER_DOWNLOAD_CACHE:
        await query.edit_message_text("❌ Session expired. Send link again.")
        return

    cache_data = USER_DOWNLOAD_CACHE[user_id]
    url = cache_data["url"]
    raw_url = cache_data["raw_url"]
    user_msg_id = cache_data["user_msg_id"]
    prompt_msg_id = cache_data["prompt_msg_id"]
    fmt = query.data

    chat_id = query.message.chat.id

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
    except Exception:
        pass

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
    except Exception:
        pass

    progress_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Downloading.")
    anim_task = asyncio.create_task(animate_download(progress_msg))

    temp_dir = tempfile.mkdtemp()
    try:
        loop = asyncio.get_event_loop()
        is_audio = (fmt == "fmt_mp3")
        is_photo = (fmt == "fmt_photo")

        output_template = os.path.join(temp_dir, "media.%(ext)s")

        if is_photo:
            saved_photos = []
            title = "TikTok Photo"
            
            def fetch_tikwm():
                try:
                    r = requests.get(f"https://www.tikwm.com/api/?url={raw_url}", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                    if r.status_code == 200:
                        data = r.json().get("data", {})
                        images = data.get("images", [])
                        t_title = data.get("title", "TikTok Photo")
                        return images, t_title
                except Exception as err:
                    logger.error(f"TikWM error: {err}")
                return [], "TikTok Photo"

            images, title = await loop.run_in_executor(None, fetch_tikwm)

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            if images:
                for idx, img_url in enumerate(images):
                    try:
                        resp = requests.get(img_url, headers=headers, timeout=10)
                        if resp.status_code == 200:
                            img_path = os.path.join(temp_dir, f"photo_{idx}.jpg")
                            with open(img_path, "wb") as f:
                                f.write(resp.content)
                            saved_photos.append(img_path)
                    except Exception:
                        pass

            if not saved_photos:
                ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
                def extract_yt():
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                        return info.get("thumbnails", []), info.get("title", "TikTok Photo")
                
                thumbnails, title = await loop.run_in_executor(None, extract_yt)
                for idx, t in enumerate(thumbnails):
                    p_url = t.get("url")
                    if p_url:
                        try:
                            resp = requests.get(p_url, headers=headers, timeout=10)
                            if resp.status_code == 200:
                                img_path = os.path.join(temp_dir, f"photo_{idx}.jpg")
                                with open(img_path, "wb") as f:
                                    f.write(resp.content)
                                saved_photos.append(img_path)
                        except Exception:
                            pass

            if not saved_photos:
                raise Exception("Could not extract photos.")

            if len(saved_photos) == 1:
                with open(saved_photos[0], "rb") as pf:
                    await context.bot.send_photo(chat_id=chat_id, photo=pf, caption=f"🖼 {title}")
            else:
                media_group = []
                for i, img_p in enumerate(saved_photos[:10]):
                    caption_str = f"🖼 {title}" if i == 0 else ""
                    media_group.append(InputMediaPhoto(media=open(img_p, "rb"), caption=caption_str))
                await context.bot.send_media_group(chat_id=chat_id, media=media_group)

        elif is_audio:
            ydl_opts = {
                "format": "ba/b/bestaudio/best",
                "outtmpl": output_template,
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android_creator", "android", "tv_embedded", "mweb"]
                    }
                },
                "concurrent_fragment_downloads": 8,
                "buffersize": 1024 * 1024,
                "http_chunk_size": 10485760,
                "quiet": True,
                "no_warnings": True,
            }

            def download_audio():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    title = info.get("title", "Media")
                    uploader = info.get("uploader", "Downloader")
                    duration = info.get("duration", 0)
                    path = ydl.prepare_filename(info)
                    return path, title, uploader, duration

            downloaded_path, title, uploader, duration = await loop.run_in_executor(None, download_audio)

            actual_files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if os.path.isfile(os.path.join(temp_dir, f))]
            if not actual_files:
                raise Exception("Audio download failed.")

            final_file = actual_files[0]
            with open(final_file, "rb") as media_file:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=media_file,
                    title=title[:60],
                    performer=uploader[:30],
                    duration=duration,
                    caption=f"🎵 {title}",
                    read_timeout=300,
                    write_timeout=300,
                )

        else:
            ydl_opts = {
                "format": "b/bestvideo+bestaudio/best",
                "outtmpl": output_template,
                "merge_output_format": "mp4",
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android_creator", "android", "tv_embedded", "mweb"]
                    }
                },
                "concurrent_fragment_downloads": 8,
                "buffersize": 1024 * 1024,
                "http_chunk_size": 10485760,
                "quiet": True,
                "no_warnings": True,
            }

            def download_video():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    title = info.get("title", "Media")
                    uploader = info.get("uploader", "Downloader")
                    duration = info.get("duration", 0)
                    width = info.get("width", 0)
                    height = info.get("height", 0)
                    path = ydl.prepare_filename(info)
                    return path, title, uploader, duration, width, height

            downloaded_path, title, uploader, duration, width, height = await loop.run_in_executor(None, download_video)

            actual_files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if os.path.isfile(os.path.join(temp_dir, f))]
            if not actual_files:
                raise Exception("Video download failed.")

            final_file = actual_files[0]
            file_size_mb = os.path.getsize(final_file) / (1024 * 1024)

            if file_size_mb > MAX_FILE_SIZE_MB:
                anim_task.cancel()
                await progress_msg.edit_text(f"⚠️ File exceeds limit ({MAX_FILE_SIZE_MB}MB).")
                return

            with open(final_file, "rb") as media_file:
                send_kwargs = {
                    "chat_id": chat_id,
                    "video": media_file,
                    "caption": f"🎬 {title}",
                    "supports_streaming": True,
                    "read_timeout": 300,
                    "write_timeout": 300,
                }
                if duration:
                    send_kwargs["duration"] = int(duration)
                if width:
                    send_kwargs["width"] = int(width)
                if height:
                    send_kwargs["height"] = int(height)

                await context.bot.send_video(**send_kwargs)

    except Exception as e:
        logger.error(f"Error: {e}")
        try:
            await progress_msg.edit_text(f"❌ Download failed: {str(e)}")
        except Exception:
            pass
    finally:
        anim_task.cancel()
        try:
            await anim_task
        except Exception:
            pass
        try:
            await progress_msg.delete()
        except Exception:
            pass
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN is missing.")
        return

    print("🚀 Starting Telegram Downloader Bot (@ldo4nbot) [RENDER CLOUD ENGINE]...")
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(300)
        .write_timeout(300)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
