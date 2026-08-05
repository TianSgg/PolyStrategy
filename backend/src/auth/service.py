"""JWT authentication service and auth database helpers."""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional, List

from shared.db import get_db_connection
from shared.time_utils import UTC8_DB_NOW_SQL, format_utc8

logger = logging.getLogger(__name__)

AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "weathertaker_auth")
AUTH_SESSION_DAYS = int(os.getenv("AUTH_SESSION_DAYS", "7"))
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "0") == "1"
APP_ENV = os.getenv("APP_ENV", "development").lower()
AUTH_JWT_SECRET = os.getenv("AUTH_JWT_SECRET")
if not AUTH_JWT_SECRET:
    if APP_ENV in ("prod", "production"):
        raise RuntimeError("AUTH_JWT_SECRET is required in production")
    AUTH_JWT_SECRET = "dev-weathertaker-change-me"
    logger.warning("[Auth] AUTH_JWT_SECRET not set; using development-only default secret")


@dataclass
class UserRecord:
    id: int
    username: str
    role: str
    enabled: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


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
        user = self.get_user_by_username(username)
        if not user:
            return None
        full = self._get_user_full_by_username(username)
        if not full or not full.get("enabled"):
            return None
        if not verify_password(password, full.get("password_hash") or ""):
            return None
        return user

    def get_user_by_id(self, user_id: int) -> Optional[UserRecord]:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT id, username, role, enabled, created_at, updated_at
                   FROM users WHERE id = %s""",
                (user_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return UserRecord(
                id=row[0],
                username=row[1],
                role=row[2],
                enabled=bool(row[3]),
                created_at=format_utc8(row[4]),
                updated_at=format_utc8(row[5]),
            )
        finally:
            conn.close()

    def get_user_by_username(self, username: str) -> Optional[UserRecord]:
        full = self._get_user_full_by_username(username)
        if not full:
            return None
        return UserRecord(
            id=full["id"],
            username=full["username"],
            role=full["role"],
            enabled=bool(full["enabled"]),
            created_at=format_utc8(full.get("created_at")),
            updated_at=format_utc8(full.get("updated_at")),
        )

    def _get_user_full_by_username(self, username: str) -> Optional[dict]:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT id, username, password_hash, role, enabled, created_at, updated_at
                   FROM users WHERE username = %s""",
                (username.strip(),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            columns = [col[0] for col in cursor.description]
            return dict(zip(columns, row))
        finally:
            conn.close()

    def list_users(self) -> List[dict]:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT id, username, role, enabled, created_at, updated_at
                   FROM users ORDER BY id ASC"""
            )
            columns = [col[0] for col in cursor.description]
            users = [dict(zip(columns, row)) for row in cursor.fetchall()]
            for user in users:
                user["created_at"] = format_utc8(user.get("created_at"))
                user["updated_at"] = format_utc8(user.get("updated_at"))
            return users
        finally:
            conn.close()

    def create_user(self, username: str, password: str, role: str = "user") -> int:
        username = username.strip()
        role = role if role in ("root", "admin", "user") else "user"
        if not username:
            raise ValueError("username is required")
        if len(password) < 6:
            raise ValueError("password must be at least 6 characters")
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """INSERT INTO users (username, password_hash, role, enabled)
                   VALUES (%s, %s, %s, 1)""",
                (username, hash_password(password), role),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def set_user_enabled(self, user_id: int, enabled: bool) -> bool:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""UPDATE users SET enabled = %s, updated_at = {UTC8_DB_NOW_SQL}
                   WHERE id = %s""",
                (1 if enabled else 0, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def update_password(self, user_id: int, password: str) -> bool:
        if len(password) < 6:
            raise ValueError("password must be at least 6 characters")
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""UPDATE users SET password_hash = %s, updated_at = {UTC8_DB_NOW_SQL}
                   WHERE id = %s""",
                (hash_password(password), user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def update_username(self, user_id: int, username: str) -> bool:
        import pymysql.err
        username = username.strip()
        if not username:
            raise ValueError("username is required")
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""UPDATE users SET username = %s, updated_at = {UTC8_DB_NOW_SQL}
                   WHERE id = %s""",
                (username, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        except pymysql.err.IntegrityError:
            raise ValueError("用户名已存在")
        finally:
            conn.close()

    def delete_user_and_transfer(self, user_id: int, to_user_id: int) -> bool:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            for table in ("accounts", "leaders", "copy_trading_configs"):
                cursor.execute(
                    f"UPDATE {table} SET owner_user_id = %s WHERE owner_user_id = %s",
                    (to_user_id, user_id),
                )
            cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
            return deleted
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


_auth_service: Optional[AuthService] = None


def get_auth_service() -> AuthService:
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthService()
    return _auth_service
