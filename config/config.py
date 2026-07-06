import os
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.getenv("WEBHOOK_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
SECRET_KEY = os.getenv("SECRET_KEY", "flora-vpn-dev-secret")
PLATEGA_API_URL = os.getenv("PLATEGA_API_URL", "https://app.platega.io")
PLATEGA_MERCHANT_ID = os.getenv("PLATEGA_MERCHANT_ID")
PLATEGA_SECRET = os.getenv("PLATEGA_SECRET")
PLATEGA_RETURN_URL = os.getenv("PLATEGA_RETURN_URL")
PLATEGA_FAILED_URL = os.getenv("PLATEGA_FAILED_URL")