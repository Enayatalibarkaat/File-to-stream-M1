# bot.py (FINAL UPDATED VERSION)

import os
import asyncio
from pyrogram import Client
from config import Config

# Dictionaries for multi-client setup
multi_clients = {}
work_loads = {}

# Pyrogram ko batao ki saare handlers 'plugins' folder ke andar hain
plugins = dict(root="plugins")

# Bot client ko define karo
bot = Client(
    name="SimpleStreamBot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
    workers=100,
    plugins=plugins,  # Yahan plugins ko register kiya gaya hai
    workdir="sessions" # Session files ke liye alag folder
)

# --- Central Helper Function ---
# Yeh function ab yahan hai taaki dusri files ise yahan se import kar sakein
def get_readable_file_size(size_in_bytes):
    """ Human-readable file size return karta hai (e.g., 10.24 MB). """
    if not size_in_bytes: return '0B'
    power = 1024
    n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G'}
    while size_in_bytes >= power and n < len(power_labels):
        size_in_bytes /= power
        n += 1
    return f"{size_in_bytes:.2f} {power_labels[n]}B"


# --- Multi-Client Initialization Logic ---
class TokenParser:
    """ Environment variables se MULTI_TOKENs ko parse karta hai. """
    @staticmethod
    def parse_from_env():
        return {
            c + 1: t
            for c, (_, t) in enumerate(
                filter(lambda n: n[0].startswith("MULTI_TOKEN"), sorted(os.environ.items()))
            )
        }

async def start_client(client_id, bot_token):
    """ Ek naye client bot ko start karta hai. """
    try:
        print(f"Attempting to start Client: {client_id}")
        # Multi-clients ko updates (messages) handle karne ki zaroorat nahi hai
        client = await Client(
            name=str(client_id), 
            api_id=Config.API_ID, 
            api_hash=Config.API_HASH,
            bot_token=bot_token, 
            no_updates=True, 
            in_memory=True # Session file disk par save nahi hogi
        ).start()
        work_loads[client_id] = 0
        multi_clients[client_id] = client
        print(f"✅ Client {client_id} started successfully.")
    except Exception as e:
        print(f"!!! CRITICAL ERROR: Failed to start Client {client_id} - Error: {e}")

async def initialize_clients(main_bot_instance):
    """ Saare additional clients ko initialize karta hai. """
    multi_clients[0] = main_bot_instance
    work_loads[0] = 0
    print("Main bot instance registered for work.")

    all_tokens = TokenParser.parse_from_env()
    if not all_tokens:
        print("No additional clients found. Using default bot only.")
        return
    
    print(f"Found {len(all_tokens)} extra clients. Starting them with a delay...")
    for i, token in all_tokens.items():
        await start_client(i, token)
        # Chota sa delay taaki Telegram rate limit hit na ho
        await asyncio.sleep(2)

    if len(multi_clients) > 1:
        print(f"Multi-Client Mode Enabled. Total Clients: {len(multi_clients)}")
