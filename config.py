"""Configuration for news_nlp_tw."""
import os

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "5274395356")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# Claude model for event extraction (haiku for cost efficiency)
EXTRACTION_MODEL = "claude-haiku-4-5-20251001"

# MOPS settings
MOPS_RATE_LIMIT_SEC = 1.0  # 1 request/second
MOPS_CACHE_TTL_HOURS = 24

# Magnitude threshold for Telegram notification
NOTIFY_MAGNITUDE_THRESHOLD = 6  # only notify for high-magnitude events
