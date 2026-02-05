# app.py - PART 1
import os, asyncio, secrets, traceback, uvicorn, re, logging, httpx, urllib.parse, math
from contextlib import asynccontextmanager
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.file_id import FileId
from pyrogram import raw
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from config import Config
from database import db

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    try:
        await bot.start()
        me = await bot.get_me()
        Config.BOT_USERNAME = me.username
        multi_clients[0] = bot
        work_loads[0] = 0
        await initialize_clients()
        await bot.get_chat(Config.STORAGE_CHANNEL)
        print("✅ Bot is Live!")
    except Exception as e: print(f"Startup Error: {e}")
    yield
    if bot.is_initialized: await bot.stop()

app = FastAPI(lifespan=lifespan)
bot = Client("SimpleStreamBot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN, in_memory=True)
multi_clients = {}; work_loads = {}; class_cache = {}

async def start_client(client_id, bot_token):
    try:
        client = await Client(name=str(client_id), api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=bot_token, no_updates=True, in_memory=True).start()
        work_loads[client_id] = 0; multi_clients[client_id] = client
    except: pass

async def initialize_clients():
    tokens = {c + 1: t for c, (_, t) in enumerate(filter(lambda n: n[0].startswith("MULTI_TOKEN"), sorted(os.environ.items())))}
    for i, token in tokens.items(): await start_client(i, token)
        # app.py - PART 2
async def get_shortlink(url):
    shortener = await db.get_shortener()
    if not shortener: return url
    api_url = shortener['api_url'].strip().replace('[', '').replace(']', '')
    api_key = shortener['api_key'].strip().replace('[', '').replace(']', '')
    try:
        async with httpx.AsyncClient() as client:
            request_url = f"{api_url}?api={api_key}&url={urllib.parse.quote(url)}"
            res = await client.get(request_url, timeout=10)
            data = res.json()
            if data.get("status") == "success": return data.get("shortenedUrl") or data.get("shortlink")
            return data.get("shortlink") or data.get("shortenedUrl") or url
    except: return url

@bot.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    await message.reply_text(f"👋 **Hello!** Send me a file for a direct download link.")

@bot.on_message(filters.command("help") & filters.private)
async def help_command(client, message):
    await message.reply_text("🚀 **Commands:**\n\n🔹 `/add_channel [ID]`\n🔹 `/set_shortener [URL] [KEY]`\n🔹 `/del_shortener`\n\n**Note:** Don't use [ ] brackets in commands!")

@bot.on_message(filters.command(["add_channel", "remove_channel"]) & filters.user(Config.OWNER_ID))
async def manage_channels(client, message):
    if len(message.command) < 2: return
    cid = int(message.command[1])
    if "add" in message.command[0]: await db.add_channel(cid); await message.reply("✅ Added!")
    else: await db.remove_channel(cid); await message.reply("❌ Removed!")

@bot.on_message(filters.command("set_shortener") & filters.user(Config.OWNER_ID))
async def set_short(client, message):
    if len(message.command) < 3: return
    await db.set_shortener(message.command[1], message.command[2])
    await message.reply("✅ Shortener Updated!")

@bot.on_message(filters.command("del_shortener") & filters.user(Config.OWNER_ID))
async def del_short(client, message):
    await db.del_shortener(); await message.reply("❌ Shortener Removed!")
    # app.py - PART 3
async def handle_file_upload(message: Message):
    try:
        sent = await message.copy(chat_id=Config.STORAGE_CHANNEL)
        uid = secrets.token_urlsafe(8); await db.save_link(uid, sent.id)
        media = message.document or message.video or message.audio
        safe_name = "".join(c for c in (media.file_name or "file") if c.isalnum() or c in ('.','_','-')).strip()
        
        # Direct Download Link (Chrome Link)
        long_url = f"{Config.BASE_URL}/dl/{sent.id}/{safe_name}"
        final_link = await get_shortlink(long_url)
        
        await message.reply_text(f"**✅ File Uploaded!**\n\n📥 **Download Link:**\n`{final_link}`", 
                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📥 Download Now", url=final_link)]]))
    except: await message.reply_text("Error processing file.")

@bot.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def file_handler(_, m: Message): await handle_file_upload(m)

@bot.on_message(filters.channel & (filters.document | filters.video | filters.audio))
async def channel_handler(client, m: Message):
    if not await db.is_channel_allowed(m.chat.id): return
    try:
        sent = await m.copy(chat_id=Config.STORAGE_CHANNEL)
        media = m.document or m.video or m.audio
        safe_name = "".join(c for c in (media.file_name or "file") if c.isalnum() or c in ('.','_','-')).strip()
        long_url = f"{Config.BASE_URL}/dl/{sent.id}/{safe_name}"
        final_link = await get_shortlink(long_url)
        
        cap = m.caption.html if m.caption else f"**{media.file_name}**"
        await client.edit_message_caption(m.chat.id, m.id, f"{cap}\n\n🚀 **Direct Link:** {final_link}")
    except: pass

@app.get("/")
async def health(): return {"status": "ok"}

@app.get("/dl/{mid}/{fname}")
async def stream(r:Request, mid:int, fname:str):
    cid = min(work_loads, key=work_loads.get)
    c = multi_clients[cid]
    msg = await c.get_messages(Config.STORAGE_CHANNEL, mid)
    m = msg.document or msg.video or msg.audio
    # Streaming logic as before...
    # (Yahan aapka purana ByteStreamer logic rahega jo chrome download handle karta hai)
    return StreamingResponse(None) # Placeholder

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
    
