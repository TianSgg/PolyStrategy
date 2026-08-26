"""Auth service data types."""
from dataclasses import dataclass
from typing import List, Optional


ROLE_HIERARCHY = {"root": 2, "user": 1}


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


@dataclass
class UserRecord:
    id: int
    username: str
    role: str
    enabled: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
