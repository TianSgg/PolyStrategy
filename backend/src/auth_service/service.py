"""AuthService — JWT 签发/验证 + 密码哈希 + 认证逻辑。"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Optional, List

from auth_service.dao import AuthDao
from auth_service.types import UserRecord

logger = logging.getLogger(__name__)

AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "polystrategy_auth")
AUTH_SESSION_DAYS = int(os.getenv("AUTH_SESSION_DAYS", "7"))
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "0") == "1"
APP_ENV = os.getenv("APP_ENV", "development").lower()
AUTH_JWT_SECRET = os.getenv("AUTH_JWT_SECRET")
if not AUTH_JWT_SECRET:
    if APP_ENV in ("prod", "production"):
        raise RuntimeError("AUTH_JWT_SECRET is required in production")
    AUTH_JWT_SECRET = "dev-polystrategy-change-me"
    logger.warning("[Auth] AUTH_JWT_SECRET not set; using development-only default secret")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 260000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iterations_raw, salt, digest_hex = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


class AuthService:
    def create_token(self, user: UserRecord) -> str:
        now = int(time.time())
        exp = now + AUTH_SESSION_DAYS * 24 * 60 * 60
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role,
            "iat": now,
            "exp": exp,
        }
        signing_input = (
            _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
            + "."
            + _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        )
        signature = hmac.new(AUTH_JWT_SECRET.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
        return signing_input + "." + _b64url_encode(signature)

    def decode_token(self, token: str) -> Optional[dict]:
        try:
            header_raw, payload_raw, signature_raw = token.split(".", 2)
            signing_input = f"{header_raw}.{payload_raw}"
            expected = hmac.new(AUTH_JWT_SECRET.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
            actual = _b64url_decode(signature_raw)
            if not hmac.compare_digest(expected, actual):
                return None
            payload = json.loads(_b64url_decode(payload_raw).decode("utf-8"))
            if int(payload.get("exp", 0)) < int(time.time()):
                return None
            return payload
        except Exception:
            return None

    def authenticate(self, username: str, password: str) -> Optional[UserRecord]:
        user = AuthDao.get_user_by_username(username)
        if not user:
            return None
        full = AuthDao.get_user_full_by_username(username)
        if not full or not full.get("enabled"):
            return None
        if not verify_password(password, full.get("password_hash") or ""):
            return None
        return user

    def get_user_by_id(self, user_id: int) -> Optional[UserRecord]:
        return AuthDao.get_user_by_id(user_id)

    def get_user_by_username(self, username: str) -> Optional[UserRecord]:
        return AuthDao.get_user_by_username(username)

    def list_users(self) -> List[dict]:
        return AuthDao.list_users()

    def create_user(self, username: str, password: str, role: str = "user") -> int:
        username = username.strip()
        role = role if role in ("root", "user") else "user"
        if not username:
            raise ValueError("username is required")
        if len(password) < 6:
            raise ValueError("password must be at least 6 characters")
        return AuthDao.insert_user(username, hash_password(password), role)

    def set_user_enabled(self, user_id: int, enabled: bool) -> bool:
        return AuthDao.set_user_enabled(user_id, enabled)

    def update_password(self, user_id: int, password: str) -> bool:
        if len(password) < 6:
            raise ValueError("password must be at least 6 characters")
        return AuthDao.update_password(user_id, hash_password(password))

    def update_username(self, user_id: int, username: str) -> bool:
        username = username.strip()
        if not username:
            raise ValueError("username is required")
        return AuthDao.update_username(user_id, username)

    def delete_user_and_transfer(self, user_id: int, to_user_id: int) -> bool:
        return AuthDao.delete_user_and_transfer(user_id, to_user_id)


_auth_service: Optional[AuthService] = None


def get_auth_service() -> AuthService:
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthService()
    return _auth_service
