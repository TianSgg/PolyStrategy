"""基于 Traefik ForwardAuth 注入 header 的鉴权依赖。

下游服务通过 Traefik ForwardAuth 验证后，从以下 headers 读取用户身份：
  - X-User-Id
  - X-User-Role
  - X-Username
"""
from dataclasses import dataclass
from typing import List, Optional

from fastapi import HTTPException, Request, status


@dataclass(frozen=True)
class AuthUser:
    id: int
    username: str
    role: str
    enabled: bool

    @property
    def is_root(self) -> bool:
        return self.role == "root"

    def can_view(self, owner_user_id: int) -> bool:
        if self.is_root:
            return True
        return owner_user_id == self.id

    def visible_user_ids(self) -> Optional[List[int]]:
        if self.is_root:
            return None
        return [self.id]

    def to_dict(self) -> dict:
        return {"id": self.id, "username": self.username, "role": self.role, "enabled": self.enabled}


def get_current_user(request: Request) -> AuthUser:
    user_id = request.headers.get("X-User-Id")
    user_role = request.headers.get("X-User-Role")
    username = request.headers.get("X-Username")

    if not (user_id and user_role and username):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    return AuthUser(
        id=int(user_id),
        username=username,
        role=user_role,
        enabled=True,
    )


def require_root(request: Request) -> AuthUser:
    user = get_current_user(request)
    if not user.is_root:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Root required")
    return user
