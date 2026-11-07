import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
_upload_dir = Path(os.getenv("UPLOAD_DIR") or "files").expanduser()
UPLOAD_DIR = (_upload_dir if _upload_dir.is_absolute() else PROJECT_ROOT / _upload_dir).resolve()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)
