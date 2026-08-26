"""FastAPI auth dependencies."""
from dataclasses import dataclass
from typing import List, Optional

from fastapi import Depends, HTTPException, Request, status

from .service import AUTH_COOKIE_NAME, get_auth_service

ROLE_HIERARCHY = {"root": 3, "admin": 2, "user": 1}


@dataclass(frozen=True)
class AuthUser:
    id: int
    username: str
    role: str
    enabled: bool

    @property
    def is_root(self) -> bool:
        return self.role == "root"

    @property
    def is_admin(self) -> bool:
        return self.role in ("admin", "root")

    def can_view(self, owner_user_id: int) -> bool:
        """判断是否有权查看指定 owner 的资源"""
        if self.is_root:
            return True
        if self.role == "admin":
            if owner_user_id == self.id:
                return True
            svc = get_auth_service()
            target = svc.get_user_by_id(owner_user_id)
            return target is not None and target.role != "root"
        return owner_user_id == self.id

    def visible_user_ids(self) -> Optional[List[int]]:
        """返回当前用户可见的 owner_user_id 列表，None 表示不限"""
        if self.is_root:
            return None
        if self.role == "admin":
            svc = get_auth_service()
            user_ids = [u["id"] for u in svc.list_users() if u["role"] != "root"]
            return user_ids
        return [self.id]

    def to_dict(self) -> dict:
        return {"id": self.id, "username": self.username, "role": self.role, "enabled": self.enabled}


def _extract_token(request: Request) -> Optional[str]:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return request.cookies.get(AUTH_COOKIE_NAME)


def get_current_user(request: Request) -> AuthUser:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = get_auth_service().decode_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    user = get_auth_service().get_user_by_id(int(payload.get("sub", 0)))
    if not user or not user.enabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User disabled or not found")
    return AuthUser(id=user.id, username=user.username, role=user.role, enabled=user.enabled)


def require_admin(current_user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return current_user


def require_root(current_user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not current_user.is_root:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Root required")
    return current_user
