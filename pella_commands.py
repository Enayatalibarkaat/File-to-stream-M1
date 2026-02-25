# commands.py
# Advanced Ban System with Admin Security & HTML Formatting (Crash Proof)

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from pymongo import MongoClient
import os
import unicodedata

# --- DATABASE CONNECTION ---
MONGODB_URI = os.getenv("MONGODB_URI")
DB = os.getenv("MONGO_DB_NAME", "moviesdb")

# Get Owner ID from Env
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

client = MongoClient(MONGODB_URI)
db = client[DB]
ban_collection = db["banlist"]   # Collection to store banned words/phrases

# --- HELPER: NORMALIZE TEXT ---
def normalize_text(text: str) -> str:
    """
    Converts fancy fonts to normal text.
    Example: 𝐌𝐑.𝐁𝐔𝐋𝐋 -> mr.bull
    """
    if not text:
        return ""
    normalized = unicodedata.normalize('NFKD', text)
    return normalized.lower().strip()

# --- HELPER: CHECK AUTHORIZATION ---
def is_user_allowed(user_id: int) -> bool:
    """
    Checks if the user is the Owner OR in the allowed users list.
    """
    if user_id == OWNER_ID:
        return True
    
    doc = ban_collection.find_one({"_id": "auth_config"})
    if doc and "allowed_ids" in doc and user_id in doc["allowed_ids"]:
        return True
    
    return False

# --- COMMAND HANDLERS ---

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Security Check
    if not is_user_allowed(user_id):
        await update.message.reply_text(
            f"⛔ <b>Access Denied!</b>\n\n"
            f"You are not authorized to use this bot.\n"
            f"Your ID: <code>{user_id}</code>\n\n"
            f"To get access, please contact the owner: @captain_stive",
            parse_mode="HTML"
        )
        return

    # HTML Mode use kiya hai taki crash na ho
    await update.message.reply_text(
        "👋 Hello Enayat!\n"
        "Advanced Cleaning Bot is Active. ❤️\n\n"
        "<b>Ban Commands:</b>\n"
        "<code>/ban word</code> - Ban a word/phrase\n"
        "<code>/unban word</code> - Unban a word\n"
        "<code>/ban list</code> - Show banned words\n\n"
        "<b>User Management (Owner Only):</b>\n"
        "<code>/allowuser 12345</code> - Allow a user\n"
        "<code>/removeuser 12345</code> - Remove a user\n"
        "<code>/userlist</code> - Show allowed users",
        parse_mode="HTML"
    )

async def ban_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = update.effective_user.id

    if not is_user_allowed(user_id):
        await msg.reply_text("⛔ You are not authorized. Contact @captain_stive")
        return

    if not ctx.args:
        await msg.reply_text("❌ Please provide a word.\nUsage: <code>/ban word</code>", parse_mode="HTML")
        return

    full_input = " ".join(ctx.args)

    # --- LIST WORDS ---
    if full_input.lower() == "list":
        doc = ban_collection.find_one({"_id": "ban_config"})
        if not doc or "items" not in doc or not doc["items"]:
            await msg.reply_text("📂 Ban list is empty.")
            return

        items = doc["items"]
        # HTML safe formatting for list
        preview = "\n".join([f"• {item}" for item in items])
        await msg.reply_text(f"🚫 <b>Current Banned Items ({len(items)}):</b>\n\n{preview}", parse_mode="HTML")
        return

    # --- BAN WORD ---
    normalized_input = normalize_text(full_input)

    ban_collection.update_one(
        {"_id": "ban_config"},
        {"$addToSet": {"items": normalized_input}},
        upsert=True
    )

    await msg.reply_text(
        f"🚫 <b>Banned Successfully!</b>\n\n"
        f"Original: {full_input}\n"
        f"Saved as: <code>{normalized_input}</code>",
        parse_mode="HTML"
    )

async def unban_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = update.effective_user.id

    if not is_user_allowed(user_id):
        await msg.reply_text("⛔ You are not authorized. Contact @captain_stive")
        return

    if not ctx.args:
        await msg.reply_text("❌ Usage: <code>/unban word</code>", parse_mode="HTML")
        return

    phrase_to_remove = " ".join(ctx.args)
    normalized_phrase = normalize_text(phrase_to_remove)

    result = ban_collection.update_one(
        {"_id": "ban_config"},
        {"$pull": {"items": normalized_phrase}},
        upsert=True
    )

    if result.modified_count > 0:
        await msg.reply_text(f"✅ Unbanned: <code>{phrase_to_remove}</code>", parse_mode="HTML")
    else:
        await msg.reply_text(f"⚠️ Item not found in list: <code>{phrase_to_remove}</code>", parse_mode="HTML")

# --- ADMIN COMMANDS ---

async def allow_user_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Only Owner can use this.")
        return

    if not ctx.args:
        await update.message.reply_text("Usage: <code>/allowuser 123456</code>", parse_mode="HTML")
        return

    try:
        new_user_id = int(ctx.args[0])
        ban_collection.update_one(
            {"_id": "auth_config"},
            {"$addToSet": {"allowed_ids": new_user_id}},
            upsert=True
        )
        await update.message.reply_text(f"✅ User <code>{new_user_id}</code> allowed.", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")

async def remove_user_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Only Owner can use this.")
        return

    if not ctx.args:
        await update.message.reply_text("Usage: <code>/removeuser 123456</code>", parse_mode="HTML")
        return

    try:
        target_id = int(ctx.args[0])
        ban_collection.update_one(
            {"_id": "auth_config"},
            {"$pull": {"allowed_ids": target_id}},
            upsert=True
        )
        await update.message.reply_text(f"🚫 User <code>{target_id}</code> removed.", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")

async def user_list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    doc = ban_collection.find_one({"_id": "auth_config"})
    if not doc or "allowed_ids" not in doc or not doc["allowed_ids"]:
        await update.message.reply_text("📂 No additional users allowed.")
        return

    ids = "\n".join([f"<code>{uid}</code>" for uid in doc["allowed_ids"]])
    await update.message.reply_text(f"👥 <b>Allowed Users:</b>\n\n{ids}", parse_mode="HTML")

# --- EXPORT HANDLERS ---
def get_handlers():
    return [
        CommandHandler("start", start_cmd),
        CommandHandler("ban", ban_cmd),
        CommandHandler("unban", unban_cmd),
        CommandHandler("allowuser", allow_user_cmd),
        CommandHandler("removeuser", remove_user_cmd),
        CommandHandler("userlist", user_list_cmd)
    ]