# app.py (THE ABSOLUTE FINAL, ROCK-SOLID FULL CODE)

import os
import asyncio
import secrets
import traceback
import uvicorn
from urllib.parse import urlparse

import aiohttp
import aiofiles
from pyrogram import Client, filters, idle, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
from pyrogram.errors import FloodWait
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pyrogram.file_id import FileId
from pyrogram import raw
from pyrogram.session import Session, Auth
import math

# Imports from your other project files
from config import Config
from database import db

# --- Global Variables and Initialization ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")
bot = Client("SimpleStreamBot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN, in_memory=True)
multi_clients = {}
work_loads = {}
class_cache = {}

# --- Helper Functions ---
def get_readable_file_size(size_in_bytes):
    if not size_in_bytes: return '0B'
    power, n = 1024, 0; power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G'}
    while size_in_bytes >= power and n < len(power_labels): size_in_bytes /= power; n += 1
    return f"{size_in_bytes:.2f} {power_labels[n]}B"

def mask_filename(name: str):
    if not name: return "Protected File"; resolutions = ["2160p", "1080p", "720p", "480p", "360p"]; res_part = ""
    for res in resolutions:
        if res in name: res_part = f" {res}"; name = name.replace(res, ""); break
    base, ext = os.path.splitext(name); masked_base = ''.join(c if (i % 3 == 0 and c.isalnum()) else '*' for i, c in enumerate(base))
    return f"{masked_base}{res_part}{ext}"

# --- Pyrogram Bot Handlers ---

@bot.on_message(filters.command("start") & filters.private)
async def start_command(_, message: Message):
    user_name = message.from_user.first_name
    await message.reply_text(f"""
👋 **Hello, {user_name}!**
I generate direct download links for your files. Just send me any file or use /url to upload from a link.
""")

async def handle_file_upload(message: Message, user_id: int):
    try:
        sent_message = await message.copy(chat_id=Config.STORAGE_CHANNEL)
        unique_id = secrets.token_urlsafe(8)
        await db.save_link(unique_id, sent_message.id)
        final_link = f"{Config.BASE_URL}/show/{unique_id}"
        button = InlineKeyboardMarkup([[InlineKeyboardButton("Open Your Link 🔗", url=final_link)]])
        await message.reply_text("✅ Your shareable link has been generated!", reply_markup=button, quote=True)
    except Exception as e:
        print(f"!!! ERROR in handle_file_upload: {traceback.format_exc()}"); await message.reply_text("Sorry, something went wrong.")

@bot.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def file_handler(_, message: Message):
    await handle_file_upload(message, message.from_user.id)

@bot.on_message(filters.command("url") & filters.private & filters.user(Config.OWNER_ID))
async def url_upload_handler(_, message: Message):
    if len(message.command) < 2: await message.reply_text("Usage: `/url <link>`"); return
    url = message.command[1]; file_name = os.path.basename(urlparse(url).path) or f"file_{int(time.time())}"; status_msg = await message.reply_text("Processing...")
    file_path = os.path.join('downloads', file_name)
    if not os.path.exists('downloads'): os.makedirs('downloads')
    try:
        async with aiohttp.ClientSession() as s, s.get(url, timeout=None) as r:
            if r.status != 200: await status_msg.edit_text(f"Download failed: {r.status}"); return
            async with aiofiles.open(file_path, 'wb') as f:
                async for c in r.content.iter_chunked(1024*1024): await f.write(c)
    except Exception as e:
        await status_msg.edit_text(f"Error: {e}");
        if os.path.exists(file_path): os.remove(file_path); return
    try:
        sent_message = await bot.send_document(Config.STORAGE_CHANNEL, file_path); await handle_file_upload(sent_message, message.from_user.id); await status_msg.delete()
    finally:
        if os.path.exists(file_path): os.remove(file_path)


# --- SIMPLE & RELIABLE GATEKEEPER LOGIC ---

@bot.on_chat_member_updated(filters.chat(Config.STORAGE_CHANNEL))
async def simple_gatekeeper(client: Client, member_update: ChatMemberUpdated):
    try:
        if (member_update.new_chat_member and member_update.new_chat_member.status == enums.ChatMemberStatus.MEMBER):
            user = member_update.new_chat_member.user
            if user.id == Config.OWNER_ID or user.is_self: return
            print(f"Gatekeeper: Unauthorized user '{user.first_name}' ({user.id}) joined. Kicking...")
            await client.ban_chat_member(Config.STORAGE_CHANNEL, user.id)
            await client.unban_chat_member(Config.STORAGE_CHANNEL, user.id)
            print(f"Gatekeeper: User {user.id} kicked successfully.")
    except Exception as e: print(f"Gatekeeper Error: {e}")

async def cleanup_channel(client: Client):
    """Cleans up unauthorized members on startup (Robust Version)."""
    print("Gatekeeper: Running initial channel cleanup...")
    allowed_members = {Config.OWNER_ID, client.me.id}
    try:
        async for member in client.get_chat_members(Config.STORAGE_CHANNEL):
            if member.user.id in allowed_members: continue
            if member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]: continue
            try:
                print(f"Gatekeeper cleanup: Found unauthorized member {member.user.id}. Kicking...")
                await client.ban_chat_member(Config.STORAGE_CHANNEL, member.user.id)
                await asyncio.sleep(1)
            except FloodWait as e: await asyncio.sleep(e.value)
            except Exception as e: print(f"Gatekeeper cleanup: Could not kick {member.user.id}. Error: {e}")
        print("Gatekeeper: Initial channel cleanup complete.")
    except Exception as e: print(f"Gatekeeper cleanup: Could not get chat members list. Error: {e}")


# --- FastAPI Web Server Routes ---
class ByteStreamer:
    def __init__(self, c: Client): self.client = c
    @staticmethod
    async def get_location(f: FileId): return raw.types.InputDocumentFileLocation(id=f.media_id, access_hash=f.access_hash, file_reference=f.file_reference, thumb_size=f.thumbnail_size)
    async def yield_file(self, f: FileId, i: int, o: int, fc: int, lc: int, pc: int, cs: int):
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
@app.get("/show/{uid}",response_class=HTMLResponse)
async def show_page(r:Request,uid:str):
    mid=await db.get_link(uid)
    if not mid:raise HTTPException(404)
    mb=multi_clients.get(0);
    if not mb:raise HTTPException(503)
    fmsg=await mb.get_messages(Config.STORAGE_CHANNEL,mid);m=fmsg.document or fmsg.video or fmsg.audio
    if not m:raise HTTPException(404)
    oname=m.file_name or "file";sname="".join(c for c in oname if c.isalnum() or c in (' ','.','_','-')).rstrip()
    ctx={"request":r,"file_name":mask_filename(oname),"file_size":get_readable_file_size(m.file_size),"is_media":(m.mime_type or "").startswith(("v","a")),"direct_dl_link":f"{Config.BASE_URL}/dl/{mid}/{sname}","mx_player_link":f"intent:{Config.BASE_URL}/dl/{mid}/{sname}#Intent;action=android.intent.action.VIEW;type={m.mime_type or 'v/*'};end","vlc_player_link":f"vlc://{Config.BASE_URL}/dl/{mid}/{sname}"}
    return templates.TemplateResponse("show.html",ctx)
@app.get("/dl/{mid}/{fname}")
async def stream_media(r:Request,mid:int,fname:str):
    i=min(work_loads,key=work_loads.get,default=0);c=multi_clients.get(i)
    if not c:raise HTTPException(503)
    tc=class_cache.get(c) or ByteStreamer(c);class_cache[c]=tc
    try:
        msg=await c.get_messages(Config.STORAGE_CHANNEL,mid);m=msg.document or msg.video or msg.audio
        if not m or msg.empty:raise FileNotFoundError
        fid=FileId.decode(m.file_id);fsize=m.file_size;rh=r.headers.get("Range",0);fb,ub=0,fsize-1
        if rh:fbs,ubs=rh.replace("bytes=","").split("-");fb=int(fbs)
        if 'ubs' in locals() and ubs:ub=int(ubs)
        if(ub>=fsize)or(fb<0):raise HTTPException(416)
        rl=ub-fb+1;cs=1024*1024;off=(fb//cs)*cs;fc=fb-off;lc=(ub%cs)+1;pc=math.ceil(rl/cs)
        body=tc.yield_file(fid,i,off,fc,lc,pc,cs);sc=206 if rh else 200
        hdrs={"Content-Type":m.mime_type or "application/octet-stream","Accept-Ranges":"bytes","Content-Disposition":f'inline; filename="{m.file_name}"',"Content-Length":str(rl)}
        if rh:hdrs["Content-Range"]=f"bytes {fb}-{ub}/{fsize}"
        return StreamingResponse(body,status_code=sc,headers=hdrs)
    except FileNotFoundError:raise HTTPException(404)
    except Exception:print(traceback.format_exc());raise HTTPException(500)

# --- Main Execution Block ---

async def main():
    """Starts Bot and Web Server together."""
    print("--- Initializing Services ---")
    
    await db.connect()
    
    try:
        print("Starting main bot...")
        await bot.start()
    except FloodWait as e:
        print(f"!!! FloodWait of {e.value}s received. Sleeping..."); await asyncio.sleep(e.value + 5); await bot.start()
        
    print(f"Bot [@{bot.me.username}] started successfully.")
    
    try:
        print(f"Verifying channel access for {Config.STORAGE_CHANNEL}...")
        await bot.get_chat(Config.STORAGE_CHANNEL)
        print("✅ Channel is accessible.")
    except Exception as e:
        print(f"\n❌❌❌ FATAL: Could not access STORAGE_CHANNEL. Error: {e}\n"); return

    # --- ROBUST STARTUP CLEANUP ---
    try:
        await cleanup_channel(bot)
    except Exception as e:
        print(f"Warning: Initial channel cleanup failed, but continuing startup. Error: {e}")

    multi_clients[0] = bot
    work_loads[0] = 0

    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    
    print("\nStarting web server...")
    print("✅✅✅ All services are up and running! ✅✅✅\n")
    
    loop = asyncio.get_event_loop()
    bot_task = loop.create_task(idle())
    web_server_task = loop.create_task(server.serve())
    await asyncio.gather(bot_task, web_server_task)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutdown signal received.")
    finally:
        print("--- Services are shutting down ---")
        if bot.is_initialized:
            asyncio.run(bot.stop())
        print("Shutdown complete.")
