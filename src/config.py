import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

RESPONSE_MODEL = os.getenv("RESPONSE_MODEL")
RESPONSE_PROVIDER = os.getenv("RESPONSE_PROVIDER", "google").strip().lower()
GOOGLE_RESPONSE_MODEL = os.getenv("GOOGLE_RESPONSE_MODEL", "gemini-2.5-flash")
GOOGLE_STRONG_RESPONSE_MODEL = os.getenv(
    "GOOGLE_STRONG_RESPONSE_MODEL",
    "gemini-3.5-flash",
)
AFFECTION_MODEL = os.getenv("AFFECTION_MODEL")
MEMORY_EXTRACT_MODEL = os.getenv("MEMORY_EXTRACT_MODEL", "gemma-4-31b-it")

MEMORY_DB_PATH = os.getenv("MEMORY_DB_PATH", "memory.sqlite3")
MEMORY_EMBEDDING_MODEL = os.getenv("MEMORY_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
MEMORY_TOP_K = int(os.getenv("MEMORY_TOP_K", "5"))
MEMORY_MIN_SCORE = float(os.getenv("MEMORY_MIN_SCORE", "0.55"))
MEMORY_DEDUP_MIN_SCORE = float(os.getenv("MEMORY_DEDUP_MIN_SCORE", "0.98"))
MEMORY_MIN_IMPORTANCE = int(os.getenv("MEMORY_MIN_IMPORTANCE", "1"))
MEMORY_EXTRACT_CONTEXT_MESSAGES = int(os.getenv("MEMORY_EXTRACT_CONTEXT_MESSAGES", "5"))
AFFECTION_PATH = os.getenv("AFFECTION_PATH", "affection.csv")

BRAVE_API_KEY = os.getenv("BRAVE_API_KEY")
BRAVE_MAX_RESULTS = int(os.getenv("BRAVE_MAX_RESULTS", "5"))
BRAVE_TIMEOUT = float(os.getenv("BRAVE_TIMEOUT", "20"))
VISIT_PAGE_TIMEOUT = float(os.getenv("VISIT_PAGE_TIMEOUT", "20"))
VISIT_PAGE_MAX_CHARS = int(os.getenv("VISIT_PAGE_MAX_CHARS", "8000"))
VISIT_PAGE_MAX_BYTES = int(os.getenv("VISIT_PAGE_MAX_BYTES", "1000000"))
CHANNEL_HISTORY_DEFAULT_LIMIT = int(
    os.getenv("CHANNEL_HISTORY_DEFAULT_LIMIT", "50")
)
CHANNEL_HISTORY_MAX_RESULTS = int(os.getenv("CHANNEL_HISTORY_MAX_RESULTS", "200"))
CHANNEL_HISTORY_MAX_SCAN = int(os.getenv("CHANNEL_HISTORY_MAX_SCAN", "1000"))
CHANNEL_HISTORY_MAX_CHARS = int(os.getenv("CHANNEL_HISTORY_MAX_CHARS", "12000"))
SEND_FILE_MAX_CHARS = int(os.getenv("SEND_FILE_MAX_CHARS", "100000"))

WIKIPEDIA_DIR = os.getenv("WIKIPEDIA_DIR", "data/wikipedia")
WIKIPEDIA_INDEX_URL = os.getenv(
    "WIKIPEDIA_INDEX_URL",
    "https://lb.download.kiwix.org/zim/wikipedia/",
)
WIKIPEDIA_ZIM_PREFIX = os.getenv("WIKIPEDIA_ZIM_PREFIX", "wikipedia_en_all_mini")
WIKIPEDIA_MAX_RESULTS = int(os.getenv("WIKIPEDIA_MAX_RESULTS", "5"))
WIKIPEDIA_MAX_CHARS = int(os.getenv("WIKIPEDIA_MAX_CHARS", "8000"))

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
RESPONSE_ANALYTICS_PATH = os.getenv("RESPONSE_ANALYTICS_PATH", "response_analytics.jsonl")
MUSIC_TELEMETRY_PATH = os.getenv("MUSIC_TELEMETRY_PATH", "music_telemetry.sqlite3")
REMINDERS_DB_PATH = os.getenv("REMINDERS_DB_PATH", "reminders.sqlite3")
REMINDERS_MAX_PER_USER = int(os.getenv("REMINDERS_MAX_PER_USER", "25"))
CANVAS_DB_PATH = os.getenv("CANVAS_DB_PATH", "canvas.sqlite3")
CANVAS_ENCRYPTION_KEY = os.getenv("CANVAS_ENCRYPTION_KEY", "").strip()
CANVAS_SUMMARY_TIME = os.getenv("CANVAS_SUMMARY_TIME", "07:00").strip()
CANVAS_DEFAULT_TIMEZONE = os.getenv(
    "CANVAS_DEFAULT_TIMEZONE", "America/Los_Angeles"
).strip()
CANVAS_DEFAULT_LOOKAHEAD_DAYS = int(
    os.getenv("CANVAS_DEFAULT_LOOKAHEAD_DAYS", "1")
)
CANVAS_MAX_LOOKAHEAD_DAYS = int(os.getenv("CANVAS_MAX_LOOKAHEAD_DAYS", "30"))
CANVAS_CACHE_SECONDS = int(os.getenv("CANVAS_CACHE_SECONDS", "300"))
CANVAS_FETCH_TIMEOUT = float(os.getenv("CANVAS_FETCH_TIMEOUT", "20"))
CANVAS_FETCH_MAX_BYTES = int(os.getenv("CANVAS_FETCH_MAX_BYTES", "2000000"))
PERSONA_DB_PATH = os.getenv("PERSONA_DB_PATH", "data/persona.sqlite3")
PERSONA_DASHBOARD_PASSWORD = os.getenv(
    "PERSONA_DASHBOARD_PASSWORD",
    "",
).strip()
MUSIC_WEB_HOST = os.getenv("MUSIC_WEB_HOST", "0.0.0.0")
MUSIC_WEB_PORT = int(os.getenv("MUSIC_WEB_PORT", "8080"))
MUSIC_WEB_BASE_URL = os.getenv("MUSIC_WEB_BASE_URL", "").rstrip("/")
MUSIC_UPLOAD_DIR = os.getenv("MUSIC_UPLOAD_DIR", "data/music_uploads")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
