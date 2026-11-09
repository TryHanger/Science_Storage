import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file if present
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = BASE_DIR / "contracts"
EXCHANGE_DIR = BASE_DIR / "exchange"

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgrespassword@127.0.0.1:5434/research_engine",
)

OPENALEX_API_KEY = os.getenv("OPENALEX_API_KEY", "").strip() or None
OPENALEX_MAILTO = os.getenv("OPENALEX_MAILTO", "researcher@example.com").strip()
