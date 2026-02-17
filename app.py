import os, asyncio, traceback, uvicorn, re, httpx, urllib.parse, math, tempfile, subprocess
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import imageio_ffmpeg
from pyrogram import Client, filters, raw
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.file_id import FileId
from pyrogram.session import Session, Auth
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse

from config import Config
from database import db

QUALITY_PATTERN = re.compile(r"(?<!\d)(2160|1440|1080|720|480|360|240)p(?!\d)", re.IGNORECASE)
STRIP_TOKENS_PATTERN = re.compile(
    r"\b(2160p|1440p|1080p|720p|480p|360p|240p|x264|x265|hevc|hdrip|webrip|web[- ]?dl|bluray|dvdrip|10bit|8bit|esubs|dual[- ]?audio|amzn|nf|hindi|english|tamil|telugu|malayalam)\b",
    re.IGNORECASE,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    try:
        await bot.start()
        Config.BOT_USERNAME = (await bot.get_me()).username
        multi_clients[0] = bot
        work_loads[0] = 0
        await initialize_clients()
        await bot.get_chat(Config.STORAGE_CHANNEL)
        print("✅ Bot is Live and Ready!")
    except Exception as e:
        print(f"Startup Error: {e}")
    yield
    if bot.is_initialized:
        await bot.stop()


app = FastAPI(lifespan=lifespan)
bot = Client("SimpleStreamBot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN, in_memory=True)
multi_clients = {}
work_loads = {}
class_cache = {}
screenshot_locks = {}


async def start_client(client_id, bot_token):
    try:
        client = await Client(name=str(client_id), api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=bot_token, no_updates=True, in_memory=True).start()
        work_loads[client_id] = 0
        multi_clients[client_id] = client
    except Exception:
        traceback.print_exc()


async def initialize_clients():
    tokens = {c + 1: t for c, (_, t) in enumerate(filter(lambda n: n[0].startswith("MULTI_TOKEN"), sorted(os.environ.items())))}
    for i, token in tokens.items():
        await start_client(i, token)


async def get_shortlink(url):
    shortener = await db.get_shortener()
    if not shortener:
        return url
    api_url = shortener['api_url'].strip().replace('[', '').replace(']', '')
    api_key = shortener['api_key'].strip().replace('[', '').replace(']', '')
    try:
        async with httpx.AsyncClient() as client:
            request_url = f"{api_url}?api={api_key}&url={urllib.parse.quote(url)}"
            res = await client.get(request_url, timeout=15)
            data = res.json()
            short_url = data.get("shortenedUrl") or data.get("shortlink") or data.get("url")
            if short_url:
                return short_url
    except Exception:
        pass
    return url


def extract_quality(text: str) -> int:
    if not text:
        return 0
    found = QUALITY_PATTERN.findall(text)
    if not found:
        return 0
    return max(int(x) for x in found)


def extract_movie_key(file_name: str, caption: str = "") -> str:
    source = (file_name or caption or "unknown_movie").lower().replace("_", " ").replace(".", " ")
    source = STRIP_TOKENS_PATTERN.sub(" ", source)
    source = re.sub(r"\b\d{3,4}p\b", " ", source)
    source = re.sub(r"\s+", " ", source).strip()
    return source[:120] or "unknown_movie"


def get_video_duration_seconds(video_path: str) -> float:
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run(
        [ffmpeg_bin, "-i", video_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    output = result.stderr or ""
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        return 0.0
    h, m, sec = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(sec)


def capture_screenshots(video_path: str, output_dir: str, count: int = 7):
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    duration = get_video_duration_seconds(video_path)
    if duration <= 0:
        return []

    start = duration * 0.10
    end = duration * 0.90
    if end <= start:
        return []

    step = (end - start) / count
    saved = []

    for idx in range(1, count + 1):
        ts = start + (idx - 1) * step
        out_file = os.path.join(output_dir, f"screenshot_{idx}.jpg")
        cmd = [
            ffmpeg_bin,
            "-ss", f"{ts:.3f}",
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", "3",
            "-y",
            out_file,
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode == 0 and os.path.exists(out_file) and os.path.getsize(out_file) > 0:
            saved.append(out_file)

    return saved


async def generate_and_store_screenshots(media: Message, storage_message_id: int):
    media_obj = media.document or media.video or media.audio
    name_for_key = media_obj.file_name if media_obj else ""
    cap_text = media.caption.html if media.caption else ""
    movie_key = extract_movie_key(name_for_key, cap_text)
    quality = max(extract_quality(name_for_key), extract_quality(cap_text))
    if quality <= 0:
        print(f"[screenshots] Skipped: quality not found for message {storage_message_id}")
        return

    lock = screenshot_locks.setdefault(movie_key, asyncio.Lock())
    async with lock:
        try:
            existing = await db.get_movie_screenshots(movie_key)
            existing_q = int(existing.get("best_quality", 0)) if existing else 0
            existing_links = existing.get("screenshot_links", []) if existing else []
            if existing_q >= quality and len(existing_links) >= 6:
                print(f"[screenshots] Skipped: existing quality {existing_q} >= {quality} for '{movie_key}'")
                return

            storage_msg = await bot.get_messages(Config.STORAGE_CHANNEL, storage_message_id)
            storage_media = storage_msg.document or storage_msg.video or storage_msg.audio
            if not storage_media:
                print(f"[screenshots] Skipped: storage media missing for message {storage_message_id}")
                return

            with tempfile.TemporaryDirectory(prefix="shots_") as tmpdir:
                source_file = os.path.join(tmpdir, "source_video")
                await bot.download_media(storage_msg, file_name=source_file)

                paths = await asyncio.to_thread(capture_screenshots, source_file, tmpdir, 7)
                if len(paths) < 6:
                    print(f"[screenshots] Skipped: only {len(paths)} screenshots captured for '{movie_key}'")
                    return

                screenshot_links = []
                for i, p in enumerate(paths, start=1):
                    sent_img = await bot.send_document(
                        chat_id=Config.STORAGE_CHANNEL,
                        document=p,
                        file_name=f"{movie_key.replace(' ', '_')}_{quality}p_{i}.jpg",
                        caption=f"Screenshot {i} | {movie_key} | {quality}p",
                    )
                    img_name = f"{movie_key.replace(' ', '_')}_{quality}p_{i}.jpg"
                    screenshot_links.append(f"{Config.BASE_URL}/dl/{sent_img.id}/{img_name}")

            payload = {
                "movie_key": movie_key,
                "best_quality": quality,
                "source_message_id": storage_message_id,
                "screenshot_links": screenshot_links,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
            await db.upsert_movie_screenshots(movie_key, payload)
            print(f"[screenshots] Saved {len(screenshot_links)} screenshots for '{movie_key}' ({quality}p)")
        except Exception:
            print(f"[screenshots] Error for '{movie_key}'")
            traceback.print_exc()


@bot.on_message(filters.command("start") & filters.private)
async def start_cmd(client, m):
    await m.reply_text("👋 Hello! Send me a file to get a direct download link.")


@bot.on_message(filters.command("help") & filters.private)
async def help_cmd(client, m):
    await m.reply_text("🚀 **Admin Commands:**\n\n🔹 `/add_channel [ID]`\n🔹 `/set_shortener [API_URL] [API_KEY]`\n🔹 `/del_shortener`")


@bot.on_message(filters.command(["add_channel", "remove_channel"]) & filters.user(Config.OWNER_ID))
async def chan_manage(client, m):
    if len(m.command) < 2:
        return
    try:
        cid = int(m.command[1])
        if "add" in m.command[0]:
            await db.add_channel(cid)
            await m.reply("✅ Channel Added!")
        else:
            await db.remove_channel(cid)
            await m.reply("❌ Channel Removed!")
    except Exception:
        await m.reply("Invalid ID.")


@bot.on_message(filters.command("set_shortener") & filters.user(Config.OWNER_ID))
async def set_short_cmd(client, m):
    if len(m.command) < 3:
        return
    await db.set_shortener(m.command[1], m.command[2])
    await m.reply("✅ Shortener Updated!")


@bot.on_message(filters.command("del_shortener") & filters.user(Config.OWNER_ID))
async def del_short_cmd(client, m):
    await db.del_shortener()
    await m.reply("❌ Shortener Deleted!")


class ByteStreamer:
    def __init__(self, c: Client):
        self.client = c

    @staticmethod
    async def get_location(f: FileId):
        return raw.types.InputDocumentFileLocation(id=f.media_id, access_hash=f.access_hash, file_reference=f.file_reference, thumb_size=f.thumbnail_size)

    async def yield_file(self, f: FileId, i: int, o: int, fc: int, lc: int, pc: int, cs: int):
        c = self.client
        work_loads[i] += 1
        if f.dc_id not in c.media_sessions:
            if f.dc_id != await c.storage.dc_id():
                ak = await Auth(c, f.dc_id, await c.storage.test_mode()).create()
                ms = Session(c, f.dc_id, ak, await c.storage.test_mode(), is_media=True)
                await ms.start()
                ea = await c.invoke(raw.functions.auth.ExportAuthorization(dc_id=f.dc_id))
                await ms.invoke(raw.functions.auth.ImportAuthorization(id=ea.id, bytes=ea.bytes))
                c.media_sessions[f.dc_id] = ms
            else:
                c.media_sessions[f.dc_id] = c.session

        ms = c.media_sessions[f.dc_id]
        loc = await self.get_location(f)
        cp = 1
        try:
            while cp <= pc:
                r = await ms.invoke(raw.functions.upload.GetFile(location=loc, offset=o, limit=cs), retries=2)
                if isinstance(r, raw.types.upload.File):
                    chk = r.bytes
                    if not chk:
                        break
                    if pc == 1:
                        yield chk[fc:lc]
                    elif cp == 1:
                        yield chk[fc:]
                    elif cp == pc:
                        yield chk[:lc]
                    else:
                        yield chk
                    cp += 1
                    o += cs
                else:
                    break
        finally:
            work_loads[i] -= 1


@app.get("/dl/{mid}/{fname}")
async def stream(r: Request, mid: int, fname: str):
    if not work_loads:
        raise HTTPException(503)
    cid = min(work_loads, key=work_loads.get)
    c = multi_clients[cid]
    tc = class_cache.get(c) or ByteStreamer(c)
    class_cache[c] = tc
    try:
        msg = await c.get_messages(Config.STORAGE_CHANNEL, mid)
        m = msg.document or msg.video or msg.audio
        fid = FileId.decode(m.file_id)
        fsize = m.file_size
        rh = r.headers.get("Range", "")
        fb, ub = 0, fsize - 1
        if rh:
            rps = rh.replace("bytes=", "").split("-")
            fb = int(rps[0])
            if len(rps) > 1 and rps[1]:
                ub = int(rps[1])
        rl = ub - fb + 1
        cs = 1024 * 1024
        off = (fb // cs) * cs
        fc = fb - off
        lc = (ub % cs) + 1
        pc = math.ceil(rl / cs)
        return StreamingResponse(
            tc.yield_file(fid, cid, off, fc, lc, pc, cs),
            status_code=206 if rh else 200,
            headers={
                "Content-Type": m.mime_type or "application/octet-stream",
                "Accept-Ranges": "bytes",
                "Content-Length": str(rl),
                "Content-Disposition": f'attachment; filename="{fname}"',
            },
        )
    except Exception:
        raise HTTPException(404)


async def handle_file_upload(message: Message):
    try:
        sent = await message.copy(chat_id=Config.STORAGE_CHANNEL)
        media = message.document or message.video or message.audio
        safe_name = "".join(c for c in (media.file_name or "file") if c.isalnum() or c in ('.', '_', '-')).strip()
        long_url = f"{Config.BASE_URL}/dl/{sent.id}/{safe_name}"
        final_link = await get_shortlink(long_url)
        await message.reply_text(
            f"**✅ File Uploaded!**\n\n📥 **Download Link:**\n`{final_link}`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📥 Download Now", url=final_link)]]),
        )
    except Exception:
        await message.reply_text("Error processing file.")


@bot.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def private_handler(_, m):
    await handle_file_upload(m)


@bot.on_message(filters.channel & (filters.document | filters.video | filters.audio))
async def channel_handler(client, m):
    if not await db.is_channel_allowed(m.chat.id):
        return
    try:
        sent = await m.copy(chat_id=Config.STORAGE_CHANNEL)
        media = m.document or m.video or m.audio
        safe_name = "".join(c for c in (media.file_name or "file") if c.isalnum() or c in ('.', '_', '-')).strip()
        final_link = await get_shortlink(f"{Config.BASE_URL}/dl/{sent.id}/{safe_name}")
        cap = m.caption.html if m.caption else f"**{media.file_name}**"
        await client.edit_message_caption(m.chat.id, m.id, f"{cap}\n\n🚀 **Download:** {final_link}")

        if m.video or (m.document and (media.mime_type or "").startswith("video/")):
            asyncio.create_task(generate_and_store_screenshots(m, sent.id))
    except Exception:
        print("[channel_handler] Error while processing channel media")
        traceback.print_exc()


@app.get("/screenshots/{movie_key}")
async def get_screenshots(movie_key: str):
    key = movie_key.lower().strip()
    doc = await db.get_movie_screenshots(key)
    if not doc:
        raise HTTPException(404, "Movie screenshots not found")
    return {
        "movie_key": doc.get("movie_key", key),
        "best_quality": doc.get("best_quality", 0),
        "screenshot_links": doc.get("screenshot_links", []),
        "updatedAt": doc.get("updatedAt"),
    }


@app.api_route("/", methods=["GET", "POST", "HEAD"])
async def health(request: Request):
    return {"status": "ok", "method": request.method}


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
