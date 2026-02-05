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

    async def connect(self):
        if Config.DATABASE_URL:
            self._client = motor.motor_asyncio.AsyncIOMotorClient(Config.DATABASE_URL)
            self.db = self._client["StreamLinksDB"]
            self.links = self.db["links"]
            self.channels = self.db["channels"]
            self.settings = self.db["settings"]
            print("✅ Database Connected!")

    async def save_link(self, unique_id, message_id):
        if self.links is not None: await self.links.insert_one({'_id': unique_id, 'message_id': message_id})

    async def get_link(self, unique_id):
        if self.links is not None:
            doc = await self.links.find_one({'_id': unique_id})
            return doc.get('message_id') if doc else None
        return None

    async def add_channel(self, channel_id):
        if self.channels is not None: await self.channels.update_one({'_id': channel_id}, {'$set': {'_id': channel_id}}, upsert=True)

    async def remove_channel(self, channel_id):
        if self.channels is not None: await self.channels.delete_one({'_id': channel_id})

    async def is_channel_allowed(self, channel_id):
        if self.channels is not None:
            doc = await self.channels.find_one({'_id': channel_id})
            return doc is not None
        return False

    async def set_shortener(self, api_url, api_key):
        if self.settings is not None:
            await self.settings.update_one({'_id': 'shortener_config'}, {'$set': {'api_url': api_url, 'api_key': api_key}}, upsert=True)

    async def get_shortener(self):
        if self.settings is not None: return await self.settings.find_one({'_id': 'shortener_config'})
        return None

    async def del_shortener(self):
        if self.settings is not None: await self.settings.delete_one({'_id': 'shortener_config'})

db = Database()
