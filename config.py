import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("DATABASE_NAME", "hybridbot")
PORT = int(os.getenv("PORT", 8080))

# Dashboard / API
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
SESSION_SECRET = os.getenv("SESSION_SECRET")
DASHBOARD_URL = os.getenv("DASHBOARD_URL")
OWNER_ID = os.getenv("OWNER_ID")
