# bot.py (THE FINAL, CLEAN, WORKING CODE)

import os
import time
import asyncio
import secrets
import traceback
from urllib.parse import urlparse

import aiohttp
import aiofiles
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import Config
from database import db

# Dictionaries
multi_clients = {}
work_loads = {}

# --- Bot Initialization ---
bot = Client(
    "SimpleStreamBot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
    workers=100
)


# --- Multi-Client Initialization ---
class TokenParser:
    @staticmethod
    def parse_from_env():
        return {
            c + 1: t
            for c, (_, t) in enumerate(
                filter(lambda n: n[0].startswith("MULTI_TOKEN"), sorted(os.environ.items()))
            )
        }


async def start_client(client_id, bot_token):
    try:
        print(f"Attempting to start Client: {client_id}")
        client = await Client(
            name=str(client_id), api_id=Config.API_ID, api_hash=Config.API_HASH,
            bot_token=bot_token, no_updates=True, in_memory=True
        ).start()
        work_loads[client_id] = 0
        multi_clients[client_id] = client
        print(f"Client {client_id} started successfully.")
    except Exception as e:
        print(f"!!! CRITICAL ERROR: Failed to start Client {client_id} - Error: {e}")


async def initialize_clients(main_bot_instance):
    multi_clients[0] = main_bot_instance
    work_loads[0] = 0
    
    # --- YEH FINAL FIX HAI ---
    # Bot ko start hone ke baad thoda time do sync hone ke liye
    print("Waiting 5 seconds for bot to initialize...")
    await asyncio.sleep(5) 
    
    try:
        print(f"Fetching info for STORAGE_CHANNEL ({Config.STORAGE_CHANNEL})...")
        # Ab get_chat call karo, is baar yeh kaam karega
        await main_bot_instance.get_chat(Config.STORAGE_CHANNEL)
        print("Channel info cached successfully.")
    except Exception as e:
        print(f"!!! FATAL ERROR: Could not get channel info. Make sure bot is an admin in STORAGE_CHANNEL. Error: {e}")
        return # Agar yeh fail ho toh aage mat badho
    # --- FIX KHATAM ---

    all_tokens = TokenParser.parse_from_env()
    if not all_tokens:
        print("No additional clients found. Using default bot only."); return
    
    print(f"Found {len(all_tokens)} extra clients. Starting them with a delay.")
    for i, token in all_tokens.items():
        await start_client(i, token)
        await asyncio.sleep(5)

    if len(multi_clients) > 1:
        print(f"Multi-Client Mode Enabled. Total Clients: {len(multi_clients)}")


# --- Helper Functions ---
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
        
        final_link = f"{Config.BLOGGER_PAGE_URL}?id={unique_id}"

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
    finally:
        if os.path.exists(file_path): os.remove(file_path)

    await handle_file_upload(sent_message, message.from_user.id)
    await status_msg.delete()
