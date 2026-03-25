from pella_main import main as start_pella_bot
import os, asyncio, traceback, uvicorn, re, httpx, urllib.parse, math, tempfile, subprocess
from urllib.parse import urlsplit, urlunsplit
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
from pella_commands import normalize_text, is_user_allowed, ban_collection, OWNER_ID

QUALITY_PATTERN = re.compile(r"(?<!\d)(2160|1440|1080|720|480|360|240)p(?!\d)", re.IGNORECASE)
STRIP_TOKENS_PATTERN = re.compile(
    r"\b(2160p|1440p|1080p|720p|480p|360p|240p|x264|x265|hevc|hdrip|webrip|web[- ]?dl|bluray|dvdrip|brrip|10bit|8bit|esubs|dual[- ]?audio|amzn|nf|hindi|english|tamil|telugu|malayalam|fhd|uhd|org|mov|moviesmod|moviesflix|hollywood|bollywood|movie|movies|full|mkv|mp4|avi)\b|[@&#+*]",
    re.IGNORECASE,
)

SCREENSHOT_COUNT = 7
MIN_SCREENSHOT_COUNT = 6
SCREENSHOT_WORKERS = 2
DOWNLOAD_RETRIES = 3
SCREENSHOT_DEBOUNCE_SECONDS = int(os.environ.get("SCREENSHOT_DEBOUNCE_SECONDS", "90"))

def get_quality_priority(quality: int) -> int:
    """720p sabse prefer, phir 1080p, phir 480p"""
    if quality == 720: return 4
    if quality == 1080: return 3
    if quality == 480: return 2
    if quality == 360: return 1
    if quality == 2160: return 1  # 4K bahut badi file, low priority
    return 0

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".flv", ".wmv", ".ts", ".m2ts"}


def is_video_media(message: Message, media_obj) -> bool:
    if getattr(message, "video", None):
        return True
    mime = (getattr(media_obj, "mime_type", "") or "").lower()
    if mime.startswith("video/"):
        return True
    name = (getattr(media_obj, "file_name", "") or "").lower()
    return any(name.endswith(ext) for ext in VIDEO_EXTENSIONS)


def derive_screenshot_meta(media: Message):
    media_obj = media.document or media.video or media.audio
    if not media_obj:
        return None

    name_for_key = media_obj.file_name if getattr(media_obj, "file_name", None) else ""
    cap_text = media.caption.html if media.caption else ""
    movie_key = extract_movie_key(name_for_key, cap_text)

    text_quality = max(extract_quality(name_for_key), extract_quality(cap_text))
    media_quality = infer_quality_from_media(media_obj)
    quality = max(text_quality, media_quality)
    if quality <= 0 and is_video_media(media, media_obj):
        quality = 720

    source_file_size = int(getattr(media_obj, "file_size", 0) or 0)
    return {
        "movie_key": movie_key,
        "quality": quality,
        "source_file_size": source_file_size,
        "is_video": is_video_media(media, media_obj),
    }


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
        asyncio.create_task(start_pella_bot())

        # ✅ NAYA: Queue workers start karo
        for i in range(SCREENSHOT_WORKERS):
            asyncio.create_task(screenshot_queue_worker())
        log_event(f"Started {SCREENSHOT_WORKERS} screenshot queue workers")

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
screenshot_semaphore = asyncio.Semaphore(SCREENSHOT_WORKERS)
pending_screenshot_jobs = {}
pending_screenshot_jobs_lock = asyncio.Lock()

# ✅ NAYA: Screenshot queue - saari movies ek line mein wait karengi
screenshot_queue = asyncio.Queue()

# ✅ DUPLICATE FIX: Track karo kaunsi movies queue mein hain ya process ho rahi hain
queued_movie_keys: set = set()
queued_movie_keys_lock = asyncio.Lock()


def log_event(message: str):
    print(f"[bot] {message}")


def _log_task_exception(task: asyncio.Task, label: str):
    try:
        exc = task.exception()
        if exc:
            log_event(f"{label} failed: {exc}")
            traceback.print_exception(type(exc), exc, exc.__traceback__)
    except asyncio.CancelledError:
        log_event(f"{label} cancelled")
    except Exception as callback_error:
        log_event(f"{label} callback error: {callback_error}")


# ✅ NAYA: Queue worker - ek ek karke saari movies process karega
async def screenshot_queue_worker():
    """Worker jo queue se ek ek job uthata hai aur complete karta hai. Koi bhi movie skip nahi hogi."""
    while True:
        try:
            job = await screenshot_queue.get()
            log_event(f"queue worker: processing '{job['movie_key']}' ({job['quality']}p) | queue remaining: {screenshot_queue.qsize()}")
            await generate_and_store_screenshots(
                job["message"],
                job["storage_message_id"],
                movie_key_override=job["movie_key"],
                quality_override=job["quality"],
                source_file_size_override=job["source_file_size"],
            )
        except Exception as e:
            log_event(f"queue worker error: {e}")
            traceback.print_exc()
        finally:
            # ✅ DUPLICATE FIX: Processing done, ab isko set se remove karo
            async with queued_movie_keys_lock:
                queued_movie_keys.discard(job.get("movie_key", ""))
            screenshot_queue.task_done()


async def _run_debounced_screenshot_job(movie_key: str):
    await asyncio.sleep(SCREENSHOT_DEBOUNCE_SECONDS)
    async with pending_screenshot_jobs_lock:
        job = pending_screenshot_jobs.pop(movie_key, None)

    if not job:
        log_event(f"screenshots debounce: no pending job for '{movie_key}'")
        return

    log_event(
        f"screenshots debounce: adding '{movie_key}' ({job['quality']}p) to queue"
    )

    # ✅ DUPLICATE FIX: Check karo already queue mein hai ya nahi
    async with queued_movie_keys_lock:
        if job["movie_key"] in queued_movie_keys:
            log_event(f"queue: SKIP '{job['movie_key']}' already in queue or processing!")
            return
        queued_movie_keys.add(job["movie_key"])

    # ✅ NAYA: Direct call ki jagah queue mein daalo
    await screenshot_queue.put(job)
    log_event(f"queue: added '{job['movie_key']}' | queue size now: {screenshot_queue.qsize()}")


async def schedule_screenshot_job(media_message: Message, storage_message_id: int):
    meta = derive_screenshot_meta(media_message)
    if not meta:
        log_event(f"screenshots skipped: no media metadata for message {storage_message_id}")
        return

    if not meta["is_video"]:
        log_event(f"screenshots skipped: media is not video for message {storage_message_id}")
        return

    if meta["quality"] <= 0:
        log_event(f"screenshots skipped: quality not found for message {storage_message_id}")
        return

    movie_key = meta["movie_key"]

    existing_doc = await db.get_movie_screenshots(movie_key)
    if has_saved_screenshots(existing_doc):
        log_event(f"screenshots skipped: already exists for '{movie_key}'")
        return

    async with pending_screenshot_jobs_lock:
        existing = pending_screenshot_jobs.get(movie_key)
        should_replace = (
            existing is None
            or get_quality_priority(meta["quality"]) > get_quality_priority(existing["quality"])
            or (
                get_quality_priority(meta["quality"]) == get_quality_priority(existing["quality"])
                and meta["source_file_size"] > existing["source_file_size"]
            )
        )

        if should_replace:
            pending_screenshot_jobs[movie_key] = {
                "movie_key": movie_key,
                "quality": meta["quality"],
                "source_file_size": meta["source_file_size"],
                "storage_message_id": storage_message_id,
                "message": media_message,
            }
            log_event(
                f"screenshots debounce: queued '{movie_key}' => {meta['quality']}p (msg {storage_message_id})"
            )
        else:
            log_event(
                f"screenshots debounce: kept better queued job for '{movie_key}' ({existing['quality']}p)"
            )

        if not existing or not existing.get("task") or existing.get("task").done():
            task = asyncio.create_task(_run_debounced_screenshot_job(movie_key))
            task.add_done_callback(lambda t: _log_task_exception(t, f"screenshots debounce task {movie_key}"))
            pending_screenshot_jobs[movie_key]["task"] = task


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


def to_preview_link(link: str) -> str:
    if not link:
        return link
    try:
        parts = urlsplit(link)
        path = parts.path or ""
        if "/dl/" in path:
            path = path.replace("/dl/", "/view/", 1)
            return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
    except Exception:
        pass
    return link.replace("/dl/", "/view/", 1)

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


def infer_quality_from_media(media_obj) -> int:
    if not media_obj:
        return 0
    h = getattr(media_obj, "height", 0) or 0
    if h >= 2000:
        return 2160
    if h >= 1300:
        return 1440
    if h >= 900:
        return 1080
    if h >= 650:
        return 720
    if h >= 450:
        return 480
    if h >= 320:
        return 360
    if h > 0:
        return 240
    return 0


def has_saved_screenshots(existing_doc) -> bool:
    if not existing_doc:
        return False
    links = existing_doc.get("screenshot_links", [])
    return len(links) >= MIN_SCREENSHOT_COUNT


def should_refresh_screenshots(existing_doc, new_quality: int, new_size: int) -> bool:
    if not existing_doc:
        return True
    if has_saved_screenshots(existing_doc):
        return False
    return True


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
            "-loglevel", "error",
            "-ss", f"{ts:.3f}",
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", "3",
            "-y",
            out_file,
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if result.returncode == 0 and os.path.exists(out_file) and os.path.getsize(out_file) > 0:
            saved.append(out_file)

    return saved


async def generate_and_store_screenshots(
    media: Message,
    storage_message_id: int,
    movie_key_override: str = "",
    quality_override: int = 0,
    source_file_size_override: int = 0,
):
    log_event(f"screenshots task started for message {storage_message_id}")
    try:
        media_obj = media.document or media.video or media.audio
        if not media_obj and not (movie_key_override and quality_override > 0):
            log_event(f"screenshots skipped: no media object for message {storage_message_id}")
            return

        if movie_key_override and quality_override > 0:
            movie_key = movie_key_override
            quality = int(quality_override)
            source_file_size = int(source_file_size_override or 0)
        else:
            name_for_key = media_obj.file_name if getattr(media_obj, "file_name", None) else ""
            cap_text = media.caption.html if media.caption else ""
            movie_key = extract_movie_key(name_for_key, cap_text)

            text_quality = max(extract_quality(name_for_key), extract_quality(cap_text))
            media_quality = infer_quality_from_media(media_obj)
            quality = max(text_quality, media_quality)
            if quality <= 0 and is_video_media(media, media_obj):
                quality = 720
                log_event(f"screenshots quality fallback: defaulted to {quality}p for message {storage_message_id}")
            if quality <= 0:
                log_event(f"screenshots skipped: quality not found for message {storage_message_id}")
                return

            source_file_size = int(getattr(media_obj, "file_size", 0) or 0)
        lock = screenshot_locks.setdefault(movie_key, asyncio.Lock())

        async with screenshot_semaphore:
            async with lock:
                existing = await db.get_movie_screenshots(movie_key)
                if not should_refresh_screenshots(existing, quality, source_file_size):
                    log_event(f"screenshots skipped: existing set is better/equal for '{movie_key}'")
                    return

                storage_msg = await bot.get_messages(Config.STORAGE_CHANNEL, storage_message_id)
                storage_media = storage_msg.document or storage_msg.video or storage_msg.audio
                if not storage_media:
                    log_event(f"screenshots skipped: storage media missing for message {storage_message_id}")
                    return

                with tempfile.TemporaryDirectory(prefix="shots_") as tmpdir:
                    source_file = os.path.join(tmpdir, "source_video")

                    downloaded = False
                    for attempt in range(1, DOWNLOAD_RETRIES + 1):
                        try:
                            await bot.download_media(storage_msg, file_name=source_file)
                            downloaded = True
                            break
                        except Exception as download_error:
                            log_event(f"download retry {attempt}/{DOWNLOAD_RETRIES} failed for '{movie_key}': {download_error}")
                            await asyncio.sleep(1)

                    if not downloaded:
                        log_event(f"screenshots failed: could not download source for '{movie_key}'")
                        return

                    paths = await asyncio.to_thread(capture_screenshots, source_file, tmpdir, SCREENSHOT_COUNT)
                    if len(paths) < MIN_SCREENSHOT_COUNT:
                        log_event(f"screenshots skipped: only {len(paths)} captured for '{movie_key}'")
                        return

                    screenshot_links = []
                    screenshot_preview_links = []
                    for i, p in enumerate(paths, start=1):
                        sent_img = await bot.send_document(
                            chat_id=Config.STORAGE_CHANNEL,
                            document=p,
                            file_name=f"{movie_key.replace(' ', '_')}_{quality}p_{i}.jpg",
                            caption=f"Screenshot {i} | {movie_key} | {quality}p",
                        )
                        img_name = f"{movie_key.replace(' ', '_')}_{quality}p_{i}.jpg"
                        screenshot_links.append(f"{Config.BASE_URL}/dl/{sent_img.id}/{img_name}")
                        screenshot_preview_links.append(to_preview_link(screenshot_links[-1]))

                payload = {
                    "movie_key": movie_key,
                    "best_quality": quality,
                    "source_message_id": storage_message_id,
                    "source_file_size": source_file_size,
                    "screenshot_links": screenshot_links,
                    "screenshot_preview_links": screenshot_preview_links,
                    "screenshots": screenshot_preview_links,
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                }
                await db.upsert_movie_screenshots(movie_key, payload)
                log_event(f"screenshots saved: {len(screenshot_links)} for '{movie_key}' ({quality}p)")
    except Exception:
        log_event(f"screenshots fatal error for message {storage_message_id}")
        traceback.print_exc()


@bot.on_message(filters.command("start") & filters.private)
async def start_cmd(client, m):
    await m.reply_text("👋 Hello! Send me a file to get a direct download link.")


@bot.on_message(filters.command("help") & filters.private)
async def help_cmd(client, m):
    await m.reply_text("🚀 **Admin Commands:**\n\n🔹 `/add_channel [ID]`\n🔹 `/set_shortener [API_URL] [API_KEY]`\n🔹 `/del_shortener`")


@bot.on_message(filters.command("ban") & filters.private)
async def ban_word_cmd(client, m):
    user_id = m.from_user.id if m.from_user else 0
    if not is_user_allowed(user_id):
        await m.reply_text("⛔ You are not authorized. Contact @captain_stive")
        return

    if len(m.command) < 2:
        await m.reply_text("❌ Please provide a word.\nUsage: /ban word")
        return

    full_input = " ".join(m.command[1:]).strip()
    if full_input.lower() == "list":
        doc = ban_collection.find_one({"_id": "ban_config"})
        if not doc or "items" not in doc or not doc["items"]:
            await m.reply_text("📂 Ban list is empty.")
            return

        items = doc["items"]
        preview = "\n".join([f"• {item}" for item in items])
        await m.reply_text(f"🚫 Current Banned Items ({len(items)}):\n\n{preview}")
        return

    normalized_input = normalize_text(full_input)
    ban_collection.update_one(
        {"_id": "ban_config"},
        {"$addToSet": {"items": normalized_input}},
        upsert=True,
    )
    await m.reply_text(f"🚫 Banned Successfully!\n\nOriginal: {full_input}\nSaved as: {normalized_input}")


@bot.on_message(filters.command("unban") & filters.private)
async def unban_word_cmd(client, m):
    user_id = m.from_user.id if m.from_user else 0
    if not is_user_allowed(user_id):
        await m.reply_text("⛔ You are not authorized. Contact @captain_stive")
        return

    if len(m.command) < 2:
        await m.reply_text("❌ Usage: <code>/unban word</code>", parse_mode="html")
        return

    phrase_to_remove = " ".join(m.command[1:]).strip()
    normalized_phrase = normalize_text(phrase_to_remove)
    result = ban_collection.update_one(
        {"_id": "ban_config"},
        {"$pull": {"items": normalized_phrase}},
        upsert=True,
    )

    if result.modified_count > 0:
        await m.reply_text(f"✅ Unbanned: {phrase_to_remove}")
    else:
        await m.reply_text(f"⚠️ Item not found in list: {phrase_to_remove}")


@bot.on_message(filters.command("allowuser") & filters.private)
async def allow_user_private_cmd(client, m):
    user_id = m.from_user.id if m.from_user else 0
    if OWNER_ID is not None and user_id != OWNER_ID:
        await m.reply_text("⛔ Only Owner can use this.")
        return

    if len(m.command) < 2:
        await m.reply_text("Usage: /allowuser 123456")
        return

    try:
        new_user_id = int(m.command[1])
    except ValueError:
        await m.reply_text("❌ Invalid ID.")
        return

    ban_collection.update_one(
        {"_id": "auth_config"},
        {"$addToSet": {"allowed_ids": new_user_id}},
        upsert=True,
    )
    await m.reply_text(f"✅ User <code>{new_user_id}</code> allowed.", parse_mode="html")


@bot.on_message(filters.command("removeuser") & filters.private)
async def remove_user_private_cmd(client, m):
    user_id = m.from_user.id if m.from_user else 0
    if OWNER_ID is not None and user_id != OWNER_ID:
        await m.reply_text("⛔ Only Owner can use this.")
        return

    if len(m.command) < 2:
        await m.reply_text("Usage: /removeuser 123456")
        return

    try:
        target_id = int(m.command[1])
    except ValueError:
        await m.reply_text("❌ Invalid ID.")
        return

    ban_collection.update_one(
        {"_id": "auth_config"},
        {"$pull": {"allowed_ids": target_id}},
        upsert=True,
    )
    await m.reply_text(f"🚫 User {target_id} removed.")


@bot.on_message(filters.command("userlist") & filters.private)
async def user_list_private_cmd(client, m):
    user_id = m.from_user.id if m.from_user else 0
    if OWNER_ID is not None and user_id != OWNER_ID:
        return

    doc = ban_collection.find_one({"_id": "auth_config"})
    if not doc or "allowed_ids" not in doc or not doc["allowed_ids"]:
        await m.reply_text("📂 No additional users allowed.")
        return

    ids = "\n".join([str(uid) for uid in doc["allowed_ids"]])
    await m.reply_text(f"👥 Allowed Users:\n\n{ids}")


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


@bot.on_message(filters.command("status") & filters.user(Config.OWNER_ID))
async def status_cmd(client, m):
    allowed = "configured" if Config.STORAGE_CHANNEL else "missing"
    shortener = await db.get_shortener()
    await m.reply_text(
        "🧩 **Bot Status**\n"
        f"- Storage channel: {allowed} (`{Config.STORAGE_CHANNEL}`)\n"
        f"- Base URL: `{Config.BASE_URL or 'missing'}`\n"
        f"- Shortener: `{'enabled' if shortener else 'disabled'}`\n"
        f"- Screenshot workers: `{SCREENSHOT_WORKERS}`\n"
        f"- Screenshot queue size: `{screenshot_queue.qsize()}`"
    )


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


async def _stream_media(r: Request, mid: int, fname: str, inline: bool = False):
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
        disposition = "inline" if inline else "attachment"
        return StreamingResponse(
            tc.yield_file(fid, cid, off, fc, lc, pc, cs),
            status_code=206 if rh else 200,
            headers={
                "Content-Type": m.mime_type or "application/octet-stream",
                "Accept-Ranges": "bytes",
                "Content-Length": str(rl),
                "Content-Disposition": f'{disposition}; filename="{fname}"',
            },
        )
    except Exception:
        raise HTTPException(404)


@app.get("/dl/{mid}/{fname}")
async def stream(r: Request, mid: int, fname: str):
    return await _stream_media(r, mid, fname, inline=False)


@app.get("/view/{mid}/{fname}")
async def preview(r: Request, mid: int, fname: str):
    return await _stream_media(r, mid, fname, inline=True)


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
        log_event(f"channel {m.chat.id} skipped: not in allowed list")
        return
    try:
        sent = await m.copy(chat_id=Config.STORAGE_CHANNEL)
        media = m.document or m.video or m.audio
        safe_name = "".join(c for c in (media.file_name or "file") if c.isalnum() or c in ('.', '_', '-')).strip()
        final_link = await get_shortlink(f"{Config.BASE_URL}/dl/{sent.id}/{safe_name}")
        cap = m.caption.html if m.caption else f"**{media.file_name}**"
        await client.edit_message_caption(m.chat.id, m.id, f"{cap}\n\n🚀 **Download:** {final_link}")

        if is_video_media(m, media):
            log_event(f"channel {m.chat.id}: scheduling screenshots candidate for message {sent.id}")
            task = asyncio.create_task(schedule_screenshot_job(m, sent.id))
            task.add_done_callback(lambda t: _log_task_exception(t, f"screenshots scheduler task {sent.id}"))
        else:
            log_event(f"channel {m.chat.id}: media is not video for message {sent.id}")
    except Exception:
        print("[channel_handler] Error while processing channel media")
        traceback.print_exc()


@app.get("/screenshots/{movie_key}")
async def get_screenshots(movie_key: str):
    key = movie_key.lower().strip()
    doc = await db.get_movie_screenshots(key)
    if not doc:
        raise HTTPException(404, "Movie screenshots not found")
    screenshot_links = doc.get("screenshot_links", [])
    screenshot_preview_links = doc.get("screenshot_preview_links") or [
        to_preview_link(link)
        for link in screenshot_links
    ]
    screenshots = doc.get("screenshots") or screenshot_preview_links
    return {
        "movie_key": doc.get("movie_key", key),
        "best_quality": doc.get("best_quality", 0),
        "screenshot_links": screenshot_links,
        "screenshot_preview_links": screenshot_preview_links,
        "screenshots": screenshots,
        "updatedAt": doc.get("updatedAt"),
    }


@app.api_route("/", methods=["GET", "POST", "HEAD"])
async def health(request: Request):
    return {"status": "ok", "method": request.method}


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
