# webserver.py (THE REAL FINAL VERSION - NO MORE ERRORS)

import math
import traceback
import os
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pyrogram.file_id import FileId
from pyrogram import raw, Client
from pyrogram.session import Session, Auth

from config import Config
# --- YAHAN BADLAV HAI: link_db ko bot se import nahi karna ---
from bot import bot, initialize_clients, multi_clients, work_loads, get_readable_file_size
# --- YAHAN BADLAV HAI: db se link_db ko import karna (indirectly) ---
from database import db

# --- Lifespan Manager (Starts and Stops the Bot with the Server) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Web server is starting up...")
    print("Starting bot...")
    await bot.start()
    print("Main bot started.")
    print("Initializing clients...")
    await initialize_clients(bot)
    print("All clients initialized. Application startup complete.")
    yield
    print("Web server is shutting down...")
    if bot.is_initialized:
        await bot.stop()
    print("Bot stopped.")

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
class_cache = {}

# --- Health Check Route ---
@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "ok", "message": "Server is healthy and running!"}

# --- Helper function for masking filename ---
def mask_filename(name: str) -> str:
    if not name: return "Protected File"
    resolutions = ["2160p", "1080p", "720p", "480p", "360p"]
    res_part = ""
    for res in resolutions:
        if res in name: res_part = f" {res}"; name = name.replace(res, ""); break
    base, ext = os.path.splitext(name)
    masked_base = ''.join(c if (i % 3 == 0 and c.isalnum()) else '*' for i, c in enumerate(base))
    return f"{masked_base}{res_part}{ext}"

class ByteStreamer:
    def __init__(self, client: Client): self.client = client
    @staticmethod
    async def get_location(file_id: FileId): return raw.types.InputDocumentFileLocation(id=file_id.media_id, access_hash=file_id.access_hash, file_reference=file_id.file_reference, thumb_size=file_id.thumbnail_size)
    async def yield_file(self, file_id: FileId, index: int, offset: int, first_part_cut: int, last_part_cut: int, part_count: int, chunk_size: int):
        client = self.client; work_loads[index] += 1
        media_session = client.media_sessions.get(file_id.dc_id)
        if media_session is None:
            if file_id.dc_id != await client.storage.dc_id():
                auth_key = await Auth(client, file_id.dc_id, await client.storage.test_mode()).create()
                media_session = Session(client, file_id.dc_id, auth_key, await client.storage.test_mode(), is_media=True)
                await media_session.start()
                exported_auth = await client.invoke(raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id))
                await media_session.invoke(raw.functions.auth.ImportAuthorization(id=exported_auth.id, bytes=exported_auth.bytes))
            else: media_session = client.session
            client.media_sessions[file_id.dc_id] = media_session
        location = await self.get_location(file_id)
        current_part = 1
        try:
            while current_part <= part_count:
                r = await media_session.invoke(raw.functions.upload.GetFile(location=location, offset=offset, limit=chunk_size), retries=0)
                if isinstance(r, raw.types.upload.File):
                    chunk = r.bytes
                    if not chunk: break
                    if part_count == 1: yield chunk[first_part_cut:last_part_cut]
                    elif current_part == 1: yield chunk[first_part_cut:]
                    elif current_part == part_count: yield chunk[:last_part_cut]
                    else: yield chunk
                    current_part += 1; offset += chunk_size
                else: break
        finally: work_loads[index] -= 1

# --- API Routes ---
@app.get("/show/{unique_id}")
async def show_file_page(request: Request, unique_id: str):
    try:
        storage_msg_id = await db.get_link(unique_id)
        if not storage_msg_id: raise HTTPException(404, "Link expired or invalid.")
        
        main_bot = multi_clients.get(0)
        if not main_bot: raise HTTPException(503, "Bot not initialized.")
        
        file_msg = await main_bot.get_messages(Config.STORAGE_CHANNEL, storage_msg_id)
        media = file_msg.document or file_msg.video or file_msg.audio
        if not media: raise HTTPException(404, "File media not found.")
        
        original_file_name = media.file_name
        masked_name = mask_filename(original_file_name)
        file_size = get_readable_file_size(media.file_size)
        mime_type = media.mime_type or "application/octet-stream"
        is_media = mime_type.startswith("video/") or mime_type.startswith("audio/")
        
        # --- YAHAN FINAL LINK BINA FILE NAME KE HAI ---
        dl_link = f"{Config.BASE_URL}/dl/{storage_msg_id}"
        
        context = {
            "request": request, "file_name": masked_name, "file_size": file_size,
            "is_media": is_media, "direct_dl_link": dl_link,
            "mx_player_link": f"intent:{dl_link}#Intent;action=android.intent.action.VIEW;type={mime_type};end",
            "vlc_player_link": f"vlc://{dl_link}"
        }
        return templates.TemplateResponse("show.html", context)
                
    except Exception:
        print(f"Error in /show: {traceback.format_exc()}"); raise HTTPException(500)

# --- YAHAN FINAL STREAMING ROUTE BINA FILE NAME KE HAI ---
@app.get("/dl/{msg_id}")
async def stream_handler(request: Request, msg_id: int):
    range_header = request.headers.get("Range", 0)
    
    index = min(work_loads, key=work_loads.get, default=0)
    client = multi_clients.get(index)
    if not client: raise HTTPException(503, "No available clients to stream.")

    if client in class_cache:
        tg_connect = class_cache[client]
    else:
        tg_connect = ByteStreamer(client)
        class_cache[client] = tg_connect

    try:
        message = await client.get_messages(Config.STORAGE_CHANNEL, msg_id)
        if not (message.video or message.document or message.audio) or message.empty:
            raise FileNotFoundError

        media = message.document or message.video or message.audio
        file_id = FileId.decode(media.file_id)
        file_size = media.file_size
        
        from_bytes, until_bytes = 0, file_size - 1
        if range_header:
            from_bytes_str, until_bytes_str = range_header.replace("bytes=", "").split("-")
            from_bytes = int(from_bytes_str)
            if until_bytes_str:
                until_bytes = int(until_bytes_str)

        if (until_bytes >= file_size) or (from_bytes < 0): raise HTTPException(416)

        chunk_size = 1024 * 1024
        req_length = until_bytes - from_bytes + 1
        offset = from_bytes - (from_bytes % chunk_size)
        first_part_cut = from_bytes - offset
        last_part_cut = (until_bytes % chunk_size) + 1
        part_count = math.ceil(req_length / chunk_size)

        body = tg_connect.yield_file(file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size)

        return StreamingResponse(
            content=body,
            status_code=206 if range_header else 200,
            headers={
                "Content-Type": media.mime_type or "application/octet-stream",
                "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
                "Content-Length": str(req_length),
                "Accept-Ranges": "bytes",
                "Content-Disposition": f'inline; filename="{media.file_name}"'
            },
        )
    except FileNotFoundError:
        raise HTTPException(404, "File not found on Telegram.")
    except Exception:
        print(f"Error in /dl: {traceback.format_exc()}"); raise HTTPException(500)
