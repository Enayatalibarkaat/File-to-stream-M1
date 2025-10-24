# config.py (FULL UPDATED CODE)

import os
from dotenv import load_dotenv

load_dotenv(".env")

class Config:
    API_ID = int(os.environ.get("API_ID", 0))
    API_HASH = os.environ.get("API_HASH", "")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    OWNER_ID = int(os.environ.get("OWNER_ID", 0))
    
    # ID ke bajaye ab LINK use karenge
    LOG_CHANNEL_LINK = os.environ.get("LOG_CHANNEL_LINK", "")
    STORAGE_CHANNEL_LINK = os.environ.get("STORAGE_CHANNEL_LINK", "")
    
    BASE_URL = os.environ.get("BASE_URL", "").rstrip('/')
    DATABASE_URL = os.environ.get("DATABASE_URL", "")
    BLOGGER_PAGE_URL = os.environ.get("BLOGGER_PAGE_URL", "")
    
    # Yeh variables hum code ke andar set karenge
    LOG_CHANNEL = 0
    STORAGE_CHANNEL = 0
