# config.py (FULL UPDATED CODE)

import os
from dotenv import load_dotenv

load_dotenv(".env")

class Config:
    API_ID = int(os.environ.get("API_ID"))
    API_HASH = os.environ.get("API_HASH")
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    OWNER_ID = int(os.environ.get("OWNER_ID"))
    
    LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL"))
    STORAGE_CHANNEL = int(os.environ.get("STORAGE_CHANNEL"))
    
    BASE_URL = os.environ.get("BASE_URL").rstrip('/')
    
    # Yeh nayi line add hui hai
    DATABASE_URL = os.environ.get("DATABASE_URL")
