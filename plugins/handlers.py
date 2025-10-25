# plugins/handlers.py

import os
import time
import secrets
import traceback
from urllib.parse import urlparse

import aiohttp
import aiofiles
from pyrogram import filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import Config
from database import db
from bot import bot # Sabse zaroori: bot ko import karo taaki decorators kaam karein

# Helper function yahan rahega kyunki yeh handlers use karte hain
def get_readable_file_size(size_in_bytes):
    if not size_in_bytes: return '0B'
    power, n = 1024, 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G'}
    while size_in_bytes >= power and n < len(power_labels):
        size_in_bytes /= power; n += 1
    return f"{size_in_bytes:.2f} {power_labels[n]}B"


# --- Bot Handlers ---
@bot.on_message(filters.command("start") & filters.private)
async def start_command(client, message: Message):
    user_name = message.from_user.first_name
    start_text = f"""
👋 **Hello, {user_name}!**

Welcome to Sharing Box Bot. I can help you create permanent, shareable links for your files.

**How to use me:**
1.  **Send me any file:** Just send or forward any file to this chat.
2.  **Send me a URL:** Use the `/url <direct_download_link>` command to upload from a link.

I will instantly give you a special link that you can share with anyone!
"""
    await message.reply_text(start_text)


async def handle_file_upload(message: Message, user_id: int):
    try:
        sent_message = await message.copy(chat_id=Config.STORAGE_CHANNEL)
        unique_id = secrets.token_urlsafe(8)
        
        await db.save_link(unique_id, sent_message.id)
        
        final_link = f"{Config.BLOGGER_PAGE_URL}?id={unique_id}" if Config.BLOGGER_PAGE_URL else f"{Config.BASE_URL}/show/{unique_id}"

        button = InlineKeyboardMarkup(
            [[InlineKeyboardButton(text="Open Your Link 🔗", url=final_link)]]
        )

        await message.reply_text(
            text=f"✅ Your shareable link has been generated!",
            reply_markup=button,
            quote=True
        )
    except Exception as e:
        print(f"!!! ERROR in handle_file_upload: {traceback.format_exc()}")
        await message.reply_text("Sorry, something went wrong.")


@bot.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def file_handler(client, message: Message):
    await handle_file_upload(message, message.from_user.id)


@bot.on_message(filters.command("url") & filters.private & filters.user(Config.OWNER_ID))
async def url_upload_handler(client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: `/url <direct_download_link>`"); return

    url = message.command[1]
    file_name = os.path.basename(urlparse(url).path) or f"file_{int(time.time())}"
    status_msg = await message.reply_text("Processing your link...")

    if not os.path.exists('downloads'): os.makedirs('downloads')
    file_path = os.path.join('downloads', file_name)
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=None) as resp:
                if resp.status != 200:
                    await status_msg.edit_text(f"Download failed! Status: {resp.status}"); return
                
                async with aiofiles.open(file_path, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        await f.write(chunk)
    except Exception as e:
        await status_msg.edit_text(f"Download Error: {e}")
        if os.path.exists(file_path): os.remove(file_path)
        return
    
    try:
        sent_message = await client.send_document(chat_id=Config.STORAGE_CHANNEL, document=file_path)
        await handle_file_upload(sent_message, message.from_user.id)
        await status_msg.delete()
    finally:
        if os.path.exists(file_path): os.remove(file_path)
