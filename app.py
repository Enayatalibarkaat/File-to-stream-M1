import httpx # Isse install kar lena: pip install httpx

# --- LINK SHORTENER HELPER ---
async def get_shortlink(url):
    shortener = await db.get_shortener() # Database se API mangwayein
    if not shortener:
        return url
    
    api_url = shortener['api_url']
    api_key = shortener['api_key']
    
    try:
        async with httpx.AsyncClient() as client:
            # Common PPD format: https://api_url/api?api=KEY&url=URL
            request_url = f"{api_url}?api={api_key}&url={url}"
            res = await client.get(request_url, timeout=10)
            data = res.json()
            if data.get("status") == "success" or data.get("shortenedUrl"):
                return data.get("shortenedUrl") or data.get("shortlink")
    except Exception as e:
        print(f"Shortener Error: {e}")
    return url
    

# app.py - PART 1 (Imports and Lifespan)

import os
import asyncio
import secrets
import traceback
import uvicorn
import re
import logging
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
    """
    Yeh function bot ko web server ke saath start aur stop karta hai.
    """
    print("--- Lifespan: Server chalu ho raha hai... ---")
    
    await db.connect()
    
    try:
        print("Starting main Pyrogram bot...")
        await bot.start()
        
        me = await bot.get_me()
        Config.BOT_USERNAME = me.username
        print(f"✅ Main Bot [@{Config.BOT_USERNAME}] safaltapoorvak start ho gaya.")

        # --- MULTI-CLIENT STARTUP ---
        multi_clients[0] = bot
        work_loads[0] = 0
        await initialize_clients()
        
        print(f"Verifying storage channel ({Config.STORAGE_CHANNEL})...")
        await bot.get_chat(Config.STORAGE_CHANNEL)
        print("✅ Storage channel accessible hai.")

        if Config.FORCE_SUB_CHANNEL:
            try:
                print(f"Verifying force sub channel ({Config.FORCE_SUB_CHANNEL})...")
                await bot.get_chat(Config.FORCE_SUB_CHANNEL)
                print("✅ Force Sub channel accessible hai.")
            except Exception as e:
                print(f"!!! WARNING: Bot, Force Sub channel mein admin nahi hai. Error: {e}")
        
        try:
            await cleanup_channel(bot)
        except Exception as e:
            print(f"Warning: Channel cleanup fail ho gaya. Error: {e}")

        print("--- Lifespan: Startup safaltapoorvak poora hua. ---")
    
    except Exception as e:
        print(f"!!! FATAL ERROR: Bot startup ke dauraan error aa gaya: {traceback.format_exc()}")
    
    yield
    
    print("--- Lifespan: Server band ho raha hai... ---")
    if bot.is_initialized:
        await bot.stop()
    print("--- Lifespan: Shutdown poora hua. ---")
    # app.py - PART 2 (Multi-client, Helpers, and New Upload Logic)

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class HideDLFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "GET /dl/" not in record.getMessage()

logging.getLogger("uvicorn.access").addFilter(HideDLFilter())

bot = Client("SimpleStreamBot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN, in_memory=True)
multi_clients = {}; work_loads = {}; class_cache = {}

class TokenParser:
    @staticmethod
    def parse_from_env():
        return {c + 1: t for c, (_, t) in enumerate(filter(lambda n: n[0].startswith("MULTI_TOKEN"), sorted(os.environ.items())))}

async def start_client(client_id, bot_token):
    try:
        client = await Client(name=str(client_id), api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=bot_token, no_updates=True, in_memory=True).start()
        work_loads[client_id] = 0
        multi_clients[client_id] = client
    except Exception as e: print(f"Error starting client {client_id}: {e}")

async def initialize_clients():
    all_tokens = TokenParser.parse_from_env()
    if not all_tokens: return
    tasks = [start_client(i, token) for i, token in all_tokens.items()]
    await asyncio.gather(*tasks)

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
    if match:
        title_part = base[:match.start()].strip(' .-_'); metadata_part = base[match.start():]
    else: title_part = base; metadata_part = ""
    masked_title = ''.join(c if (i % 3 == 0 and c.isalnum()) else ('*' if c.isalnum() else c) for i, c in enumerate(title_part))
    return f"{masked_title} {metadata_part}{ext}".strip()

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

# --- MODIFIED: DIRECT LINK LOGIC ---
async def handle_file_upload(message: Message, user_id: int):
    try:
        sent_message = await message.copy(chat_id=Config.STORAGE_CHANNEL)
        unique_id = secrets.token_urlsafe(8)
        await db.save_link(unique_id, sent_message.id)
        
        media = message.document or message.video or message.audio
        file_name = media.file_name or "file"
        safe_file_name = "".join(c for c in file_name if c.isalnum() or c in (' ', '.', '_', '-')).rstrip()
        
        show_link = f"{Config.BASE_URL}/show/{unique_id}"
        direct_dl_link = f"{Config.BASE_URL}/dl/{sent_message.id}/{safe_file_name}"
        
        reply_text = (
            f"**✅ File Uploaded Successfully!**\n\n"
            f"📂 **File:** `{file_name}`\n\n"
            f"🌐 **Web Page Link:**\n`{show_link}`\n\n"
            f"📥 **Direct Download Link:**\n`{direct_dl_link}`"
        )
        
        button = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Open Web Page", url=show_link)],
            [InlineKeyboardButton("📥 Direct Download", url=direct_dl_link)]
        ])
        
        await message.reply_text(reply_text, reply_markup=button, quote=True)
    except Exception as e:
        print(f"!!! ERROR: {traceback.format_exc()}"); await message.reply_text("Sorry, something went wrong.")
    # app.py - PART 3 (Gatekeeper, FastAPI and Streaming)

@bot.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def file_handler(_, message: Message):
    await handle_file_upload(message, message.from_user.id)

@bot.on_chat_member_updated(filters.chat(Config.STORAGE_CHANNEL))
async def simple_gatekeeper(c: Client, m_update: ChatMemberUpdated):
    try:
        if(m_update.new_chat_member and m_update.new_chat_member.status==enums.ChatMemberStatus.MEMBER):
            u=m_update.new_chat_member.user
            if u.id==Config.OWNER_ID or u.is_self: return
            await c.ban_chat_member(Config.STORAGE_CHANNEL,u.id); await c.unban_chat_member(Config.STORAGE_CHANNEL,u.id)
    except Exception as e: print(f"Gatekeeper Error: {e}")

async def cleanup_channel(c: Client):
    allowed={Config.OWNER_ID,c.me.id}
    try:
        async for m in c.get_chat_members(Config.STORAGE_CHANNEL):
            if m.user.id in allowed or m.status in [enums.ChatMemberStatus.ADMINISTRATOR,enums.ChatMemberStatus.OWNER]: continue
            await c.ban_chat_member(Config.STORAGE_CHANNEL,m.user.id); await asyncio.sleep(1)
    except Exception as e: print(f"Cleanup Error: {e}")

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "Server is healthy and running!"}

@app.get("/show/{unique_id}", response_class=HTMLResponse)
async def show_page(request: Request, unique_id: str):
    return templates.TemplateResponse("show.html", {"request": request})

@app.get("/api/file/{unique_id}", response_class=JSONResponse)
async def get_file_details_api(request: Request, unique_id: str):
    message_id = await db.get_link(unique_id)
    if not message_id: raise HTTPException(404, "Link expired.")
    main_bot = multi_clients.get(0)
    try:
        message = await main_bot.get_messages(Config.STORAGE_CHANNEL, message_id)
        media = message.document or message.video or message.audio
        file_name = media.file_name or "file"
        safe_file_name = "".join(c for c in file_name if c.isalnum() or c in (' ', '.', '_', '-')).rstrip()
        mime_type = media.mime_type or "application/octet-stream"
        return {
            "file_name": mask_filename(file_name), "file_size": get_readable_file_size(media.file_size),
            "is_media": mime_type.startswith(("video", "audio")),
            "direct_dl_link": f"{Config.BASE_URL}/dl/{message_id}/{safe_file_name}",
            "mx_player_link": f"intent:{Config.BASE_URL}/dl/{message_id}/{safe_file_name}#Intent;action=android.intent.action.VIEW;type={mime_type};end",
            "vlc_player_link": f"intent:{Config.BASE_URL}/dl/{message_id}/{safe_file_name}#Intent;action=android.intent.action.VIEW;type={mime_type};package=org.videolan.vlc;end"
        }
    except Exception: raise HTTPException(404, "File not found.")

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
                         
