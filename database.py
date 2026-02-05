# database.py (FINAL UPDATED VERSION)

import motor.motor_asyncio
from config import Config

class Database:
    def __init__(self):
        self._client = None
        self.db = None
        self.links = None
        self.channels = None
        self.settings = None
        if not Config.DATABASE_URL:
            print("WARNING: DATABASE_URL not set. Features like channel edit and shortener will not work.")

    async def connect(self):
        """Database se connection banata hai aur collections initialize karta hai."""
        if Config.DATABASE_URL:
            print("Connecting to the database...")
            self._client = motor.motor_asyncio.AsyncIOMotorClient(Config.DATABASE_URL)
            self.db = self._client["StreamLinksDB"]
            
            # Alag-alag kaam ke liye collections
            self.links = self.db["links"]
            self.channels = self.db["channels"]
            self.settings = self.db["settings"]
            
            print("✅ Database connection established with all collections.")
        else:
            self.db = None
            self.links = None
            self.channels = None
            self.settings = None

    async def disconnect(self):
        """Database connection ko band karta hai."""
        if self._client:
            self._client.close()
            print("Database connection closed.")

    # --- LINK STORAGE METHODS ---
    async def save_link(self, unique_id, message_id):
        if self.links is not None:
            await self.links.insert_one({'_id': unique_id, 'message_id': message_id})

    async def get_link(self, unique_id):
        if self.links is not None:
            doc = await self.links.find_one({'_id': unique_id})
            return doc.get('message_id') if doc else None
        return None

    # --- CHANNEL MANAGEMENT METHODS ---
    async def add_channel(self, channel_id):
        """Authorized channel add karne ke liye"""
        if self.channels is not None:
            await self.channels.update_one(
                {'_id': channel_id}, 
                {'$set': {'_id': channel_id}}, 
                upsert=True
            )

    async def remove_channel(self, channel_id):
        """Authorized channel remove karne ke liye"""
        if self.channels is not None:
            await self.channels.delete_one({'_id': channel_id})

    async def is_channel_allowed(self, channel_id):
        """Check karne ke liye ki kya bot is channel mein kaam karega"""
        if self.channels is not None:
            doc = await self.channels.find_one({'_id': channel_id})
            return doc is not None
        return False

    # --- SHORTENER SETTINGS METHODS ---
    async def set_shortener(self, api_url, api_key):
        """Shortener ki settings save karne ke liye"""
        if self.settings is not None:
            await self.settings.update_one(
                {'_id': 'shortener_config'},
                {'$set': {'api_url': api_url, 'api_key': api_key}},
                upsert=True
            )

    async def get_shortener(self):
        """Shortener ki settings mangwane ke liye"""
        if self.settings is not None:
            return await self.settings.find_one({'_id': 'shortener_config'})
        return None

    async def del_shortener(self):
        """Shortener ko disable karne ke liye"""
        if self.settings is not None:
            await self.settings.delete_one({'_id': 'shortener_config'})

db = Database()
