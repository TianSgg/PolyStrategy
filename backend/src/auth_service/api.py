"""Auth service API routes."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from auth_service.types import AuthUser, ROLE_HIERARCHY
from auth_service.service import AUTH_COOKIE_NAME, AUTH_COOKIE_SECURE, AUTH_SESSION_DAYS, get_auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])
forward_auth_router = APIRouter(tags=["forward-auth"])


# ==================== 鉴权依赖 ====================

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


def require_root(current_user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not current_user.is_root:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Root required")
    return current_user


# ==================== Request/Response Models ====================

class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"


class UpdateUserRequest(BaseModel):
    username: str | None = None
    enabled: bool | None = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str
    confirm_password: str


# ==================== Auth Routes ====================

def _set_auth_cookie(response: Response, token: str):
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=AUTH_SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


@router.post("/login")
async def login(data: LoginRequest, response: Response):
    user = get_auth_service().authenticate(data.username, data.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    token = get_auth_service().create_token(user)
    _set_auth_cookie(response, token)
    return {"user": {"id": user.id, "username": user.username, "role": user.role, "enabled": user.enabled}}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.get("/me")
async def me(current_user: AuthUser = Depends(get_current_user)):
    return {"user": current_user.to_dict()}


@router.get("/users")
async def list_users(_: AuthUser = Depends(require_root)):
    return {"users": get_auth_service().list_users()}


@router.post("/users")
async def create_user(data: CreateUserRequest, current_user: AuthUser = Depends(require_root)):
    if ROLE_HIERARCHY.get(data.role, 0) >= ROLE_HIERARCHY.get(current_user.role, 0):
        raise HTTPException(status_code=403, detail="只能创建比自己低的角色")
    try:
        user_id = get_auth_service().create_user(data.username, data.password, data.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"user_id": user_id}


def _check_target_permission(current_user: AuthUser, user_id: int):
    if user_id == current_user.id:
        return
    target = get_auth_service().get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if ROLE_HIERARCHY.get(target.role, 0) >= ROLE_HIERARCHY.get(current_user.role, 0):
        raise HTTPException(status_code=403, detail="只能操作比自己低级的用户")


@router.put("/users/{user_id}")
async def update_user(user_id: int, data: UpdateUserRequest, current_user: AuthUser = Depends(require_root)):
    if data.username is None and data.enabled is None:
        raise HTTPException(status_code=400, detail="No fields to update")
    if user_id == current_user.id and data.enabled is False:
        raise HTTPException(status_code=400, detail="不能禁用自己")
    _check_target_permission(current_user, user_id)
    service = get_auth_service()
    ok = True
    try:
        if data.username is not None:
            ok = service.update_username(user_id, data.username)
        if data.enabled is not None:
            ok = service.set_user_enabled(user_id, data.enabled) and ok
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "ok"}


@router.post("/change-password")
async def change_password(data: ChangePasswordRequest, current_user: AuthUser = Depends(get_current_user)):
    if data.new_password != data.confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
    service = get_auth_service()
    user = service.authenticate(current_user.username, data.old_password)
    if not user:
        raise HTTPException(status_code=400, detail="旧密码错误")
    try:
        service.update_password(current_user.id, data.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok"}


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, current_user: AuthUser = Depends(require_root)):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    _check_target_permission(current_user, user_id)
    if not get_auth_service().delete_user_and_transfer(user_id, current_user.id):
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "ok"}


@router.get("/verify")
async def verify(current_user: AuthUser = Depends(get_current_user)):
    return {"status": "ok", "user_id": current_user.id, "role": current_user.role}


# ==================== Traefik ForwardAuth ====================

@forward_auth_router.get("/auth/verify")
@forward_auth_router.head("/auth/verify")
async def forward_auth_verify(request: Request):
    token = _extract_token(request)
    if not token:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    auth_service = get_auth_service()
    payload = auth_service.decode_token(token)
    if not payload:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    user = auth_service.get_user_by_id(int(payload.get("sub", 0)))
    if not user or not user.enabled:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    return Response(
        status_code=status.HTTP_200_OK,
        headers={
            "X-User-Id": str(user.id),
            "X-User-Role": user.role,
            "X-Username": user.username,
        },
    )
