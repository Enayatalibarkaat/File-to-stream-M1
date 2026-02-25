# main.py - Part 1/5
# Updated for 7 Screenshots and Automated Metadata Support

import os
import re
import logging
import math
import unicodedata
import subprocess # --- NAYA: FFmpeg chalane ke liye ---
from datetime import datetime
from typing import Tuple, Optional, List, Dict, Any
import requests
from pymongo import MongoClient, ReturnDocument
from telegram import Update, Message
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv
from bson import ObjectId
from pella_commands import get_handlers

# Load env
load_dotenv()

# --- ENV ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
MONGODB_URI = os.getenv("MONGODB_URI")
DB = os.getenv("MONGO_DB_NAME", "moviesdb")
COL = os.getenv("MONGO_COLLECTION", "movies")

if not BOT_TOKEN or not TMDB_API_KEY or not MONGODB_URI:
    raise SystemExit("Set BOT_TOKEN, TMDB_API_KEY, MONGODB_URI in env")

# --- logging ---
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("smart-bot")

# --- Mongo ---
client = MongoClient(MONGODB_URI)
db = client[DB]
collection = db[COL]
ban_collection = db["banlist"]

# --- HELPER: File Size Formatter ---
def format_size(size_bytes: int) -> str:
    """Converts bytes into a human-readable format like MB/GB."""
    if size_bytes == 0: return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

# --- HELPER: Link Extraction ---
def extract_urls(message: Message) -> List[str]:
    """Extracts unique and valid URLs from text and entities."""
    urls = []
    text = message.caption or ""
    found = re.findall(r'(https?://[^\s()<>]+)', text)
    urls.extend(found)
    
    if message.caption_entities:
        for ent in message.caption_entities:
            if ent.type == "url":
                urls.append(text[ent.offset : ent.offset + ent.length])
            elif ent.type == "text_link":
                urls.append(ent.url)
    
    cleaned = []
    for u in urls:
        if not u: continue
        u = u.strip().rstrip('.,)]')
        if u.startswith('http') and u not in cleaned:
            cleaned.append(u)
    return cleaned
# main.py - Part 2/5

# ---------------------------------------
# RESOLUTION REMOVER
# ---------------------------------------
# Robust regex to handle cases like 480px264
RESOLUTION_TOKENS = [r"480p", r"720p", r"1080p", r"2160p", r"4k"]
RESOLUTION_REGEX = re.compile(r"\b(" + r"|".join(RESOLUTION_TOKENS) + r")", flags=re.IGNORECASE)

def clean_title_remove_resolution(title: str) -> str:
    """Removes resolution strings from the title for cleaner searching."""
    if not title: return ""
    cleaned = RESOLUTION_REGEX.sub("", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned

# -----------------------------
# Caption / quality helpers
# -----------------------------
QUALITY_TOKENS = [
    r"1080p", r"720p", r"480p", r"2160p", r"4k",
    r"bluray", r"blu[\-\s]?ray", r"hdrip", r"webrip", r"web[\-\s]?dl",
    r"x264", r"x265", r"hevc", r"10bit",
    r"uncut", r"esubs?", r"e\-?sub",
    r"dual[\-\s]?audio", r"dubbed", r"hindi", r"english", r"malayalam",
    r"tamil", r"telugu", r"kannada", r"hdtv", r"rip",
    r"amzn", r"ddp5\.1", r"aac5\.1"
]
QUALITY_REGEX = re.compile(r"\b(" + r"|".join(QUALITY_TOKENS) + r")\b", flags=re.IGNORECASE)

def remove_non_printable(s: str) -> str:
    """Filters out non-ASCII characters from the text."""
    return re.sub(r"[^\x00-\x7F]+", " ", s or "")

def normalize_spaces(s: str) -> str:
    """Reduces multiple spaces into a single space."""
    return re.sub(r"\s+", " ", (s or "")).strip()

def clean_token_regex() -> str:
    """Returns the combined regex string for quality tokens."""
    return r"|".join(QUALITY_TOKENS)

def extract_name_and_year_from_caption(caption: str) -> Tuple[str, Optional[str]]:
    """Separates movie name and release year from the caption."""
    if not caption: return "", None
    text = remove_non_printable(caption).strip()
    text = normalize_spaces(text)
    
    # Looking for a 4-digit year starting with 19 or 20
    m = re.search(r"\b(19|20)\d{2}\b", text)
    if not m:
        # If no year found, strip brackets and common symbols
        cleaned = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", " ", text)
        cleaned = re.sub(r"[^\w\s\-\.\:]", " ", cleaned)
        return normalize_spaces(cleaned), None
        
    year = m.group(0)
    idx = m.start()
    before = text[:idx].strip()
    
    # Remove technical quality tags from the title portion
    patt = re.compile(r"\b(" + clean_token_regex() + r")\b", flags=re.IGNORECASE)
    before = patt.sub(" ", before)
    before = re.sub(r"[^\w\s\-\.\:]", " ", before)
    before = normalize_spaces(before)
    
    # Proper capitalization for the title
    before_title = " ".join([w.capitalize() for w in before.split()]) if before else ""
    return before_title, year

def extract_quality_from_caption(caption: str) -> str:
    """Extracts resolution/quality keyword from the caption text."""
    if not caption: return ""
    c = remove_non_printable(caption).lower()
    
    # Priority matching for standard resolutions
    for q in ["2160p", "4k", "1080p", "720p", "480p"]:
        if q in c: return q
        
    m = QUALITY_REGEX.search(c)
    return m.group(0).strip() if m else ""
# main.py - Part 3/5

# ---------------------------------------------------
# DYNAMIC CLEANER
# ---------------------------------------------------
def get_banned_items_from_db() -> List[str]:
    """Fetches the list of banned words from the MongoDB banlist collection."""
    try:
        doc = ban_collection.find_one({"_id": "ban_config"})
        return doc["items"] if doc and "items" in doc else []
    except Exception as e:
        logger.error(f"Error fetching ban list: {e}")
        return []

def clean_caption_remove_links(text: str) -> str:
    """Removes banned phrases and cleans up the caption for TMDB searching."""
    if not text: return ""
    text = unicodedata.normalize('NFKD', text)
    banned_items = get_banned_items_from_db()
    banned_items.sort(key=len, reverse=True)
    for item in banned_items:
        if not item: continue
        pattern = re.compile(re.escape(item), flags=re.IGNORECASE)
        text = pattern.sub("", text)
    lines = text.split("\n")
    out = [line.strip() for line in lines if line.strip()]
    return "\n".join(out).strip()

# --- QUALITY SORTER HELPER ---
def get_quality_score(q_str: str) -> int:
    """Assigns a score to quality for sorting in the UI."""
    q = q_str.lower() if q_str else ""
    if "480" in q: return 1
    if "720" in q: return 2
    if "1080" in q: return 3
    if "2160" in q or "4k" in q: return 4
    return 0

# --- TMDB HELPERS (Director, Producer, Trailer Fix) ---
TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
TMDB_MOVIE_URL = "https://api.themoviedb.org/3/movie/{movie_id}"

def extract_director_producer(tmdb_detail: Dict[str, Any]) -> Tuple[str, str]:
    """Extracts Director and Producer names from TMDB credits crew list."""
    crew = tmdb_detail.get("credits", {}).get("crew", [])
    directors = [m["name"] for m in crew if m.get("job") == "Director"]
    producers = [m["name"] for m in crew if m.get("job") == "Producer"]
    return ", ".join(directors[:2]), ", ".join(producers[:2])

def extract_trailer_link(tmdb_detail: Dict[str, Any]) -> str:
    """Finds the YouTube trailer key and builds a proper Embed URL for the website."""
    videos = tmdb_detail.get("videos", {}).get("results", [])
    for v in videos:
        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
            return f"https://www.youtube.com/embed/{v.get('key')}"
    return ""

# --- NAYA: TELEGRAPH UPLOAD ---
def upload_to_telegraph(path: str) -> Optional[str]:
    """Photo ko Telegraph par upload karke uska link deta hai."""
    try:
        with open(path, 'rb') as f:
            response = requests.post(
                'https://telegra.ph/upload',
                files={'file': ('file', f, 'image/jpg')}
            ).json()
            if isinstance(response, list) and len(response) > 0:
                return "https://telegra.ph" + response[0]['src']
    except Exception as e:
        logger.error(f"Telegraph Upload Error: {e}")
    return None

# --- NAYA: 7 SCREENSHOTS CAPTURE ---
def capture_screenshots(video_url: str, movie_id: str) -> List[str]:
    """Video URL se 7 alag-alag jagah se screenshots nikalta hai."""
    screenshot_links = []
    timestamps = [
        "00:05:00", "00:15:00", "00:30:00", 
        "00:45:00", "01:00:00", "01:15:00", "01:30:00"
    ]
    for i, ts in enumerate(timestamps):
        output_file = f"ss_{movie_id}_{i}.jpg"
        cmd = [
            'ffmpeg', '-ss', ts, '-i', video_url, 
            '-frames:v', '1', '-q:v', '2', output_file, '-y'
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            link = upload_to_telegraph(output_file)
            if link: screenshot_links.append(link)
            if os.path.exists(output_file): os.remove(output_file)
        except Exception as e:
            logger.error(f"Screenshot failed at {ts}: {e}")
    return screenshot_links

def tmdb_search(query: str, year: Optional[str] = None, max_results: int = 10) -> List[Dict[str, Any]]:
    """Searches for movies on TMDB based on the extracted name and year."""
    if not query: return []
    params = {"api_key": TMDB_API_KEY, "query": query, "include_adult": False}
    if year: params["year"] = year
    try:
        r = requests.get(TMDB_SEARCH_URL, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("results", [])[:max_results]
    except Exception as e:
        logger.exception("TMDB search failed: %s", e)
        return []

def tmdb_get_details(movie_id: int) -> Optional[Dict[str, Any]]:
    """Fetches detailed metadata, including credits and videos, for a specific TMDB ID."""
    try:
        url = TMDB_MOVIE_URL.format(movie_id=movie_id)
        params = {"api_key": TMDB_API_KEY, "append_to_response": "videos,credits"}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.exception("TMDB details failed: %s", e)
        return None

def build_image_url(path: Optional[str]) -> str:
    """Constructs the full TMDB image URL for posters and backdrops."""
    return f"https://image.tmdb.org/t/p/original{path}" if path else ""
# main.py - Part 4/5

# --- SERIES ANALYZER (S1/E1 & Pack Detection) ---
def analyze_series_info(caption: str) -> Dict[str, Any]:
    """Analyzes the caption to detect if it's a series, a single episode, or a season pack."""
    cap = caption.lower()
    result = {"is_series": False, "season": 1, "type": "episode", "ep_num": 1, "title": ""}

    # 1. Check Range/Pack: S01 E01-10
    range_match = re.search(r"(?:s|season)\s?(\d+).*?(?:e|ep|episode)\s?(\d+)\s?-\s?(\d+)", cap)
    if range_match:
        result.update({
            "is_series": True,
            "season": int(range_match.group(1)),
            "type": "pack",
            "title": f"Episodes {range_match.group(2)}-{range_match.group(3)}"
        })
        return result

    # 2. Check Single Episode: S01 E01
    single_match = re.search(r"(?:s|season)\s?(\d+)\s?(?:e|ep|episode)\s?(\d+)", cap)
    if single_match:
        result.update({
            "is_series": True,
            "season": int(single_match.group(1)),
            "type": "episode",
            "ep_num": int(single_match.group(2)),
            "title": f"Episode {single_match.group(2)}"
        })
        return result

    # 3. Check Season Pack (Full Season)
    season_match = re.search(r"(?:s|season)\s?(\d+)", cap)
    if season_match:
        result.update({
            "is_series": True,
            "season": int(season_match.group(1)),
            "type": "pack",
            "title": f"Season {season_match.group(1)} (Full)"
        })
        return result

    return result

def detect_category(tmdb_detail: Dict[str, Any], caption: str, series_info: Dict) -> str:
    """Determines the movie/series category based on language and series detection."""
    if series_info["is_series"]:
        return "webseries"
        
    lang = (tmdb_detail.get("original_language") or "en").lower()
    if lang in ("hi", "hin"): return "bollywood"
    if lang in ("ta", "te", "ml", "kn"): return "south-indian"
    return "hollywood"

def choose_best_tmdb_result(results, search_name, search_year):
    """Selects the most relevant TMDB result based on name and year matching."""
    if not results: return None
    if search_year:
        matches = [r for r in results if r.get("release_date", "").startswith(search_year)]
        if matches:
            for r in matches:
                if r.get("title", "").strip().lower() == search_name.strip().lower(): return r
            return sorted(matches, key=lambda x: x.get("popularity", 0), reverse=True)[0]
    for r in results:
        if r.get("title", "").strip().lower() == search_name.strip().lower(): return r
    return results[0]

def build_mongo_document(tmdb_detail, full_caption_title, message_id_str, quality_str):
    """Creates a new MongoDB document structure matching the ZackHub schema."""
    now_iso = datetime.utcnow().isoformat() + "Z"
    series_info = analyze_series_info(full_caption_title)
    final_cat = detect_category(tmdb_detail or {}, full_caption_title, series_info)

    # Metadata helpers (Director, Producer, Trailer)
    dr_name, pr_name = extract_director_producer(tmdb_detail) if tmdb_detail else ("", "")
    tr_url = extract_trailer_link(tmdb_detail) if tmdb_detail else ""

    doc = {
        "title": full_caption_title,
        "tmdbId": tmdb_detail.get("id") if tmdb_detail else None,
        "posterUrl": build_image_url(tmdb_detail.get("poster_path")) if tmdb_detail else "",
        "backdropUrl": build_image_url(tmdb_detail.get("backdrop_path")) if tmdb_detail else "",
        "description": tmdb_detail.get("overview", "") if tmdb_detail else "",
        "category": final_cat,
        "actors": ", ".join([c["name"] for c in tmdb_detail.get("credits", {}).get("cast", [])[:10]]) if tmdb_detail else "",
        "director": dr_name,
        "producer": pr_name,
        "rating": float(tmdb_detail.get("vote_average") or 0) if tmdb_detail else 0.0,
        "downloadLinks": [],
        "telegramLinks": [],
        "seasons": [],
        "trailerLink": tr_url,
        "genres": tmdb_detail.get("genres") if tmdb_detail else [],
        "releaseDate": tmdb_detail.get("release_date", "") if tmdb_detail else "",
        "runtime": int(tmdb_detail.get("runtime") or 0) if tmdb_detail else 0,
        "tagline": tmdb_detail.get("tagline", "") if tmdb_detail else "",
        
        # --- NAYA: Screenshots Array Jagah ---
        "screenshots": [],
        # -------------------------------------

        "createdAt": now_iso,
        "updatedAt": now_iso,
        "__v": 0
    }
    return doc, series_info
# main.py - Part 5/5

# main.py - Part 5/5 (FIXED FOR LARGE FILES)

def build_lookup_key(full_caption_title: str, tmdb_release_date: str) -> Dict[str, Any]:
    """MongoDB search filter banane ke liye."""
    cleaned_title = clean_title_remove_resolution(full_caption_title)
    if tmdb_release_date:
        return {"title": cleaned_title, "releaseDate": tmdb_release_date}
    return {"title": cleaned_title}

async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.channel_post or update.edited_channel_post
        if not msg or not (msg.video or msg.document): return

        message_id_str = str(msg.message_id)
        caption = msg.caption or ""
        
        file_obj = msg.video or msg.document
        f_size_bytes = file_obj.file_size if file_obj else 0
        readable_size = format_size(f_size_bytes) if f_size_bytes > 0 else ""
        
        dl_urls = extract_urls(msg) 
        
        cleaned_caption = clean_caption_remove_links(caption)
        full_caption_title = normalize_spaces(cleaned_caption) or ""
        smart_name, year = extract_name_and_year_from_caption(cleaned_caption)
        
        raw_quality = extract_quality_from_caption(caption)
        quality_with_size = f"{raw_quality} ({readable_size})" if readable_size else raw_quality
        
        logger.info(f"Processing: {full_caption_title} | Screenshots & Metadata...")

        candidates = [(smart_name, year)] if smart_name and year else []
        if smart_name: candidates.append((smart_name, None))
        chosen_tmdb = None
        for q_title, q_year in candidates:
            res = tmdb_search(q_title, q_year)
            if res:
                chosen_tmdb = tmdb_get_details(choose_best_tmdb_result(res, q_title, q_year).get("id"))
                break

        new_doc, series_info = build_mongo_document(chosen_tmdb, clean_title_remove_resolution(full_caption_title), message_id_str, quality_with_size)
        
        if not chosen_tmdb: new_doc["releaseDate"] = year or ""

        existing = collection.find_one({"tmdbId": new_doc["tmdbId"]}) if new_doc.get("tmdbId") else None
        if not existing:
            existing = collection.find_one(build_lookup_key(new_doc["title"], year or new_doc["releaseDate"]))

        final_doc = existing if existing else new_doc
        final_doc["updatedAt"] = datetime.utcnow().isoformat() + "Z"

        # --- FIX: USE STREAM LINK FOR SCREENSHOTS ---
        if not final_doc.get("screenshots"):
            try:
                # Agar caption mein Render ya koi download link hai, toh usey use karein
                video_url = None
                if dl_urls:
                    video_url = dl_urls[0] # Pehla link uthayega
                    logger.info(f"Using Stream Link for screenshots: {video_url}")
                
                if video_url:
                    ss_links = capture_screenshots(video_url, message_id_str)
                    if ss_links:
                        final_doc["screenshots"] = ss_links
                        logger.info(f"Added {len(ss_links)} screenshots via Stream Link.")
                else:
                    logger.warning("No stream link found in caption to take screenshots.")
                    
            except Exception as e:
                logger.error(f"Screenshot Fix Failed: {e}")

        tg_link_obj = {"_id": ObjectId(), "quality": quality_with_size, "fileId": message_id_str}
        new_dl_links = [{"_id": ObjectId(), "link": url, "url": url, "quality": quality_with_size} for url in dl_urls]

        def sync_links(target_list, new_items, key):
            for item in new_items:
                url_exists = any(x.get(key) == item[key] for x in target_list)
                quality_exists = any(x.get("quality") == item["quality"] for x in target_list)
                if not url_exists and not quality_exists:
                    target_list.append(item)

        if final_doc["category"] == "webseries" and series_info["is_series"]:
            sn = series_info["season"]
            season = next((s for s in final_doc["seasons"] if s["seasonNumber"] == sn), None)
            if not season:
                season = {"seasonNumber": sn, "episodes": [], "fullSeasonFiles": []}
                final_doc["seasons"].append(season)
            
            if series_info["type"] == "pack":
                pack = next((p for p in season["fullSeasonFiles"] if p["title"] == series_info["title"]), None)
                if not pack:
                    pack = {"title": series_info["title"], "telegramLinks": [], "downloadLinks": []}
                    season["fullSeasonFiles"].append(pack)
                sync_links(pack["telegramLinks"], [tg_link_obj], "fileId")
                sync_links(pack["downloadLinks"], new_dl_links, "link")
            else:
                ep = next((e for e in season["episodes"] if e["episodeNumber"] == series_info["ep_num"]), None)
                if not ep:
                    ep = {"episodeNumber": series_info["ep_num"], "title": series_info["title"], "telegramLinks": [], "downloadLinks": []}
                    season["episodes"].append(ep)
                sync_links(ep["telegramLinks"], [tg_link_obj], "fileId")
                sync_links(ep["downloadLinks"], new_dl_links, "link")
        else:
            sync_links(final_doc.setdefault("telegramLinks", []), [tg_link_obj], "fileId")
            sync_links(final_doc.setdefault("downloadLinks", []), new_dl_links, "link")

        if existing:
            collection.replace_one({"_id": existing["_id"]}, final_doc)
        else:
            collection.insert_one(final_doc)
        logger.info(f"Done: {final_doc.get('title')} | Screenshots: {len(final_doc.get('screenshots', []))}")

    except Exception as e:
        logger.exception(f"Handle Error: {e}")

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & (filters.VIDEO | filters.Document.ALL), handle))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_CHANNEL_POST, handle))
    for h in get_handlers(): app.add_handler(h)
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Bot Active: Screenshots & Metadata Enabled!")

if __name__ == "__main__":
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.create_task(main())
    loop.run_forever()
