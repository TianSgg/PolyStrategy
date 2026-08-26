from cryptography.fernet import Fernet
from dotenv import load_dotenv
import os

load_dotenv()

def load_or_generate_key() -> bytes:
    env_key = os.environ.get("ENCRYPTION_KEY")
    if env_key:
        return env_key.encode()
    raise RuntimeError("ENCRYPTION_KEY environment variable is required")

_cipher = Fernet(load_or_generate_key())

def encrypt(text: str) -> str:
    if not text:
        return ""
    return _cipher.encrypt(text.encode()).decode()

def decrypt(encrypted: str) -> str:
    if not encrypted:
        return ""
    try:
        return _cipher.decrypt(encrypted.encode()).decode()
    except Exception:
        return ""
