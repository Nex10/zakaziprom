import os
from dotenv import load_dotenv

load_dotenv()

# Support multiple tokens separated by comma or new lines
PROM_API_TOKENS = [t.strip() for t in os.getenv("PROM_API_TOKEN", "").split(",") if t.strip()]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PROM_API_HOST = "https://my.prom.ua/api/v1"
