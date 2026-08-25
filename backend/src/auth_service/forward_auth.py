"""Traefik ForwardAuth 端点。

Traefik 将每个请求的 headers 转发到此端点：
- 200 → 请求放行，响应 headers 注入到下游请求
- 401 → 请求拒绝
"""
from fastapi import APIRouter, Request, Response, status

from auth.service import AUTH_COOKIE_NAME, get_auth_service

router = APIRouter(tags=["forward-auth"])


@router.get("/auth/verify")
@router.head("/auth/verify")
async def forward_auth_verify(request: Request, response: Response):
    """Traefik ForwardAuth 验证端点。

    从 Cookie 或 Authorization header 提取 JWT，验证后将用户信息
    通过响应 headers 注入，Traefik 会将这些 headers 转发给下游服务。
    """
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

    response.headers["X-User-Id"] = str(user.id)
    response.headers["X-User-Role"] = user.role
    response.headers["X-Username"] = user.username
    return Response(
        status_code=status.HTTP_200_OK,
        headers={
            "X-User-Id": str(user.id),
            "X-User-Role": user.role,
            "X-Username": user.username,
        },
    )


def _extract_token(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return request.cookies.get(AUTH_COOKIE_NAME, "")
