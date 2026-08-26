from cryptography.fernet import Fernet
from dotenv import load_dotenv
import os

# 加载 .env
load_dotenv()

# 优先从环境变量读取密钥，否则回退到文件（兼容已有数据）
_BACKEND_SRC = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KEY_FILE = os.path.join(_BACKEND_SRC, "account_service", "encryption.key")

def load_or_generate_key() -> bytes:
    """加载或生成加密密钥"""
    env_key = os.environ.get("ENCRYPTION_KEY")
    if env_key:
        return env_key.encode()
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            return f.read()
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(key)
    return key

# 初始化 Fernet 加密器
_cipher = Fernet(load_or_generate_key())

def encrypt(text: str) -> str:
    """加密字符串"""
    if not text:
        return ""
    return _cipher.encrypt(text.encode()).decode()

def decrypt(encrypted: str) -> str:
    """解密字符串"""
    if not encrypted:
        return ""
    try:
        return _cipher.decrypt(encrypted.encode()).decode()
    except Exception:
        return ""
