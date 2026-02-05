import os
import asyncio
import secrets
import traceback
import uvicorn
import re
import logging
import httpx # Isse requirements.txt mein add kar dena
from contextlib import asynccontextmanager

from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
from pyrogram.errors import FloodWait, UserNotParticipant
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pyrogram.file_id import FileId
from pyrogram import raw
from pyrogram.session import Session, Auth
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import math

# Project ki dusri files se important cheezein import karo
from config import Config
from database import db

# =====================================================================================
# --- SETUP: BOT, WEB SERVER, AUR LOGGING ---
# =====================================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("--- Lifespan: Server chalu ho raha hai... ---")
    await db.connect()
    try:
        print("Starting main Pyrogram bot...")
        await bot.start()
        me = await bot.get_me()
        Config.BOT_USERNAME = me.username
        print(f"✅ Main Bot [@{Config.BOT_USERNAME}] safaltapoorvak start ho gaya.")

        multi_clients[0] = bot
        work_loads[0] = 0
        await initialize_clients()
        
        print(f"Verifying storage channel ({Config.STORAGE_CHANNEL})...")
        await bot.get_chat(Config.STORAGE_CHANNEL)
        print("✅ Storage channel accessible hai.")

        if Config.FORCE_SUB_CHANNEL:
            try:
                await bot.get_chat(Config.FORCE_SUB_CHANNEL)
                print("✅ Force Sub channel accessible hai.")
            except Exception as e:
                print(f"!!! WARNING: Force Sub error: {e}")
        
        try: await cleanup_channel(bot)
        except Exception: pass

        print("--- Lifespan: Startup poora hua. ---")
    except Exception as e:
        print(f"!!! FATAL ERROR: {traceback.format_exc()}")
    yield
    if bot.is_initialized: await bot.stop()

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class HideDLFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "GET /dl/" not in record.getMessage()

logging.getLogger("uvicorn.access").addFilter(HideDLFilter())

bot = Client("SimpleStreamBot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN, in_memory=True)
multi_clients = {}; work_loads = {}; class_cache = {}

# =====================================================================================
# --- LINK SHORTENER HELPER ---
# =====================================================================================

async def get_shortlink(url):
    """PPD Shortener se link ko shorten karne ke liye"""
    shortener = await db.get_shortener()
    if not shortener:
        return url
    
    api_url = shortener['api_url']
    api_key = shortener['api_key']
    
    try:
        async with httpx.AsyncClient() as client:
            request_url = f"{api_url}?api={api_key}&url={url}"
            res = await client.get(request_url, timeout=10)
            data = res.json()
            # GPTLinks, Droplink wagera ka format handle karein
            if data.get("status") == "success" or data.get("shortenedUrl"):
                return data.get("shortenedUrl") or data.get("shortlink")
    except Exception as e:
        print(f"Shortener Error: {e}")
    return url

# =====================================================================================
# --- MULTI-CLIENT LOGIC ---
# =====================================================================================

class TokenParser:
    @staticmethod
    def parse_from_env():
        return {c + 1: t for c, (_, t) in enumerate(filter(lambda n: n[0].startswith("MULTI_TOKEN"), sorted(os.environ.items())))}

async def start_client(client_id, bot_token):
    try:
        client = await Client(name=str(client_id), api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=bot_token, no_updates=True, in_memory=True).start()
        work_loads[client_id] = 0
        multi_clients[client_id] = client
    except Exception as e: print(f"Error client {client_id}: {e}")

async def initialize_clients():
    all_tokens = TokenParser.parse_from_env()
    if not all_tokens: return
    tasks = [start_client(i, token) for i, token in all_tokens.items()]
    await asyncio.gather(*tasks)

# =====================================================================================
# --- HELPER FUNCTIONS ---
# =====================================================================================

def get_readable_file_size(size_in_bytes):
    if not size_in_bytes: return '0B'
    power = 1024; n = 0; power_labels = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB'}
    while size_in_bytes >= power and n < len(power_labels) - 1:
        size_in_bytes /= power; n += 1
    return f"{size_in_bytes:.2f} {power_labels[n]}"

def mask_filename(name: str):
    if not name: return "Protected File"
    base, ext = os.path.splitext(name)
    metadata_pattern = re.compile(r'((19|20)\d{2}|4k|2160p|1080p|720p|480p|360p|HEVC|x265|BluRay|WEB-DL|HDRip)', re.IGNORECASE)
    match = metadata_pattern.search(base)
    title_part = base[:match.start()].strip(' .-_') if match else base
    metadata_part = base[match.start():] if match else ""
    masked_title = ''.join(c if (i % 3 == 0 and c.isalnum()) else ('*' if c.isalnum() else c) for i, c in enumerate(title_part))
    return f"{masked_title} {metadata_part}{ext}".strip()

# =====================================================================================
# --- PYROGRAM BOT HANDLERS & COMMANDS ---
# =====================================================================================

@bot.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    user_id = message.from_user.id; user_name = message.from_user.first_name
    if len(message.command) > 1 and message.command[1].startswith("verify_"):
        unique_id = message.command[1].split("_", 1)[1]
        final_link = f"{Config.BASE_URL}/show/{unique_id}"
        reply_text = f"__✅ Verification Successful!\n\nCopy Link:__ `{final_link}`"
        button = InlineKeyboardMarkup([[InlineKeyboardButton("Open Link", url=final_link)]])
        await message.reply_text(reply_text, reply_markup=button, quote=True, disable_web_page_preview=True)
    else:
        await message.reply_text(f"👋 **Hello, {user_name}!**\n\nSend me any file and I will give you a direct download link instantly!")

# --- ADMIN COMMANDS FOR CHANNELS & SHORTENER ---

@bot.on_message(filters.command(["add_channel", "remove_channel"]) & filters.user(Config.OWNER_ID))
async def manage_channels(client, message):
    if len(message.command) < 2:
        return await message.reply("Please provide Channel ID (e.g. `/add_channel -1002445775054`)")
    try:
        chan_id = int(message.command[1])
        if "add" in message.command[0]:
            await db.add_channel(chan_id)
            await message.reply(f"✅ Channel `{chan_id}` added to auto-edit list!")
        else:
            await db.remove_channel(chan_id)
            await message.reply(f"❌ Channel `{chan_id}` removed!")
    except Exception as e: await message.reply(f"Error: {e}")

@bot.on_message(filters.command("set_shortener") & filters.user(Config.OWNER_ID))
async def set_short(client, message):
    if len(message.command) < 3:
        return await message.reply("Usage: `/set_shortener API_URL API_KEY`\nExample: `/set_shortener https://gplinks.in/api 123456...`")
    await db.set_shortener(message.command[1], message.command[2])
    await message.reply("✅ Shortener settings updated!")

@bot.on_message(filters.command("del_shortener") & filters.user(Config.OWNER_ID))
async def del_short(client, message):
    await db.del_shortener()
    await message.reply("❌ Shortener removed! Direct links only.")

# --- AUTO UPLOAD & CHANNEL EDIT LOGIC ---

async def handle_file_upload(message: Message, user_id: int):
    try:
        sent_message = await message.copy(chat_id=Config.STORAGE_CHANNEL)
        unique_id = secrets.token_urlsafe(8)
        await db.save_link(unique_id, sent_message.id)
        
        media = message.document or message.video or message.audio
        file_name = media.file_name or "file"
        safe_file_name = "".join(c for c in file_name if c.isalnum() or c in (' ', '.', '_', '-')).rstrip()
        
        show_link = f"{Config.BASE_URL}/show/{unique_id}"
        long_url = f"{Config.BASE_URL}/dl/{sent_message.id}/{safe_file_name}"
        
        # Link Shorten karein
        final_link = await get_shortlink(long_url)
        
        reply_text = (
            f"**✅ File Uploaded!**\n\n"
            f"📂 **File:** `{file_name}`\n\n"
            f"🌐 **Web Page Link:**\n`{show_link}`\n\n"
            f"📥 **Download Link:**\n`{final_link}`"
        )
        button = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Download Now", url=final_link)]])
        await message.reply_text(reply_text, reply_markup=button, quote=True)
    except Exception as e: print(traceback.format_exc()); await message.reply_text("Error processing file.")

@bot.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def file_handler(_, message: Message):
    await handle_file_upload(message, message.from_user.id)

@bot.on_message(filters.channel & (filters.document | filters.video | filters.audio))
async def channel_post_handler(client: Client, message: Message):
    # Check kya channel allowed hai
    if not await db.is_channel_allowed(message.chat.id):
        return
    try:
        sent_message = await message.copy(chat_id=Config.STORAGE_CHANNEL)
        unique_id = secrets.token_urlsafe(8)
        await db.save_link(unique_id, sent_message.id)

        media = message.document or message.video or message.audio
        file_name = media.file_name or "file"
        safe_file_name = "".join(c for c in file_name if c.isalnum() or c in (' ', '.', '_', '-')).rstrip()
        long_url = f"{Config.BASE_URL}/dl/{sent_message.id}/{safe_file_name}"
        
        final_link = await get_shortlink(long_url)
        
        cur_caption = message.caption.html if message.caption else f"**{file_name}**"
        new_caption = f"{cur_caption}\n\n🚀 **Download Link:** {final_link}"
        
        await client.edit_message_caption(chat_id=message.chat.id, message_id=message.id, caption=new_caption, parse_mode=enums.ParseMode.HTML)
    except Exception as e: print(f"Channel Error: {e}")

# =====================================================================================
# --- GATEKEEPER & FASTAPI ROUTES ---
# =====================================================================================

@bot.on_chat_member_updated(filters.chat(Config.STORAGE_CHANNEL))
async def simple_gatekeeper(c: Client, m_update: ChatMemberUpdated):
    try:
        if(m_update.new_chat_member and m_update.new_chat_member.status==enums.ChatMemberStatus.MEMBER):
            u=m_update.new_chat_member.user
            if u.id==Config.OWNER_ID or u.is_self: return
            await c.ban_chat_member(Config.STORAGE_CHANNEL,u.id); await c.unban_chat_member(Config.STORAGE_CHANNEL,u.id)
    except Exception: pass

async def cleanup_channel(c: Client):
    allowed={Config.OWNER_ID,c.me.id}
    try:
        async for m in c.get_chat_members(Config.STORAGE_CHANNEL):
            if m.user.id in allowed or m.status in [enums.ChatMemberStatus.ADMINISTRATOR,enums.ChatMemberStatus.OWNER]: continue
            await c.ban_chat_member(Config.STORAGE_CHANNEL,m.user.id); await asyncio.sleep(1)
    except Exception: pass

@app.get("/")
async def health_check(): return {"status": "ok"}

@app.get("/show/{unique_id}", response_class=HTMLResponse)
async def show_page(request: Request, unique_id: str):
    return templates.TemplateResponse("show.html", {"request": request})

@app.get("/api/file/{unique_id}", response_class=JSONResponse)
async def get_file_details_api(request: Request, unique_id: str):
    message_id = await db.get_link(unique_id)
    if not message_id: raise HTTPException(404)
    try:
        message = await multi_clients[0].get_messages(Config.STORAGE_CHANNEL, message_id)
        media = message.document or message.video or message.audio
        file_name = media.file_name or "file"
        safe_file_name = "".join(c for c in file_name if c.isalnum() or c in (' ', '.', '_', '-')).rstrip()
        long_url = f"{Config.BASE_URL}/dl/{message_id}/{safe_file_name}"
        final_link = await get_shortlink(long_url)
        return {
            "file_name": mask_filename(file_name), "file_size": get_readable_file_size(media.file_size),
            "is_media": (media.mime_type or "").startswith(("video", "audio")),
            "direct_dl_link": final_link, # Shortlink yahan bhi
            "mx_player_link": f"intent:{long_url}#Intent;action=android.intent.action.VIEW;type={media.mime_type};end",
            "vlc_player_link": f"intent:{long_url}#Intent;action=android.intent.action.VIEW;type={media.mime_type};package=org.videolan.vlc;end"
        }
    except Exception: raise HTTPException(404)

class ByteStreamer:
    def __init__(self,c:Client):self.client=c
    @staticmethod
    async def get_location(f:FileId): return raw.types.InputDocumentFileLocation(id=f.media_id,access_hash=f.access_hash,file_reference=f.file_reference,thumb_size=f.thumbnail_size)
    async def yield_file(self,f:FileId,i:int,o:int,fc:int,lc:int,pc:int,cs:int):
        c=self.client;work_loads[i]+=1;ms=c.media_sessions.get(f.dc_id)
        if ms is None:
            if f.dc_id!=await c.storage.dc_id():
                ak=await Auth(c,f.dc_id,await c.storage.test_mode()).create();ms=Session(c,f.dc_id,ak,await c.storage.test_mode(),is_media=True);await ms.start();ea=await c.invoke(raw.functions.auth.ExportAuthorization(dc_id=f.dc_id));await ms.invoke(raw.functions.auth.ImportAuthorization(id=ea.id,bytes=ea.bytes))
            else:ms=c.session
            c.media_sessions[f.dc_id]=ms
        loc=await self.get_location(f);cp=1
        try:
            while cp<=pc:
                r=await ms.invoke(raw.functions.upload.GetFile(location=loc,offset=o,limit=cs),retries=0)
                if isinstance(r,raw.types.upload.File):
                    chk=r.bytes
                    if not chk:break
                    if pc==1:yield chk[fc:lc]
                    elif cp==1:yield chk[fc:]
                    elif cp==pc:yield chk[:lc]
                    else:yield chk
                    cp+=1;o+=cs
                else:break
        finally:work_loads[i]-=1

@app.get("/dl/{mid}/{fname}")
async def stream_media(r:Request,mid:int,fname:str):
    if not work_loads: raise HTTPException(503)
    client_id = min(work_loads, key=work_loads.get)
    c = multi_clients.get(client_id); tc=class_cache.get(c) or ByteStreamer(c); class_cache[c]=tc
    try:
        msg=await c.get_messages(Config.STORAGE_CHANNEL,mid); m=msg.document or msg.video or msg.audio
        fid=FileId.decode(m.file_id); fsize=m.file_size; rh=r.headers.get("Range",""); fb,ub=0,fsize-1
        if rh:
            rps=rh.replace("bytes=","").split("-"); fb=int(rps[0])
            if len(rps)>1 and rps[1]: ub=int(rps[1])
        rl=ub-fb+1; cs=1024*1024; off=(fb//cs)*cs; fc=fb-off; lc=(ub%cs)+1; pc=math.ceil(rl/cs)
        return StreamingResponse(tc.yield_file(fid,client_id,off,fc,lc,pc,cs),status_code=206 if rh else 200, headers={"Content-Type":m.mime_type or "application/octet-stream","Accept-Ranges":"bytes","Content-Disposition":f'inline; filename="{m.file_name}"',"Content-Length":str(rl)})
    except Exception: raise HTTPException(500)

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="info")
    
