"""基于 Traefik ForwardAuth 注入 header 的鉴权依赖。

Traefik ForwardAuth 验证 JWT 后，将用户信息通过以下 headers 注入：
  - X-User-Id
  - X-User-Role
  - X-Username

下游服务直接读取这些 header 获取用户身份，无需自行验证 JWT。
兼容模式：如果 header 不存在，fallback 到直接 JWT 验证（过渡期）。
"""
import logging
from dataclasses import dataclass
from typing import List, Optional

from fastapi import Depends, HTTPException, Request, status

logger = logging.getLogger(__name__)

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
        if self.is_root:
            return True
        if self.role == "admin":
            return True
        return owner_user_id == self.id

    def visible_user_ids(self) -> Optional[List[int]]:
        if self.is_root:
            return None
        if self.role == "admin":
            return None
        return [self.id]

    def to_dict(self) -> dict:
        return {"id": self.id, "username": self.username, "role": self.role, "enabled": self.enabled}


def get_current_user_from_headers(request: Request) -> AuthUser:
    """从 Traefik ForwardAuth 注入的 header 中提取用户身份。

    如果 header 不存在，fallback 到 JWT 直接验证（过渡期兼容）。
    """
    user_id = request.headers.get("X-User-Id")
    user_role = request.headers.get("X-User-Role")
    username = request.headers.get("X-Username")

    if user_id and user_role and username:
        return AuthUser(
            id=int(user_id),
            username=username,
            role=user_role,
            enabled=True,
        )

    # Fallback: 直接验证 JWT（过渡期，ForwardAuth 未启用时）
    try:
        from auth.dependencies import get_current_user
        return get_current_user(request)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )


def require_admin_from_headers(request: Request) -> AuthUser:
    user = get_current_user_from_headers(request)
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user


def require_root_from_headers(request: Request) -> AuthUser:
    user = get_current_user_from_headers(request)
    if not user.is_root:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Root required")
    return user
