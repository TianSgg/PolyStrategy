"""Auth Service — 独立鉴权微服务。

职责：
- JWT 签发与验证（/api/auth/login, /api/auth/me, etc.）
- 用户管理 CRUD（/api/auth/users）
- Traefik ForwardAuth 端点（/auth/verify）
- Consul 服务注册
"""
import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth_service.api import router as auth_router, forward_auth_router
from auth_service.dao import run_auth_migrations
from auth_service.service import hash_password
from framework.consul import consul_lifespan

logger = logging.getLogger(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "polystrategy-auth")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8010"))

# ForwardAuth 鉴权地址：Traefik 用此地址校验请求身份
# 本地开发: http://127.0.0.1:8010/auth/verify
# Docker:   http://polystrategy-auth:8010/auth/verify (容器名)
AUTH_VERIFY_URL = os.getenv("AUTH_VERIFY_URL", f"http://127.0.0.1:{SERVICE_PORT}/auth/verify")

# Consul 标签：声明路由 + forwardAuth 中间件
# 中间件名 polystrategy-auth 供本项目其他服务引用 (polystrategy-auth@consulcatalog)
CONSUL_TAGS = [
    "traefik.enable=true",
    # ── 业务路由: /api/auth → 本服务 ──
    "traefik.http.routers.polystrategy-auth.rule=PathPrefix(`/api/auth`)",
    "traefik.http.routers.polystrategy-auth.entrypoints=web",
    # ── ForwardAuth 端点路由: /auth/verify → 本服务 (不加鉴权，Traefik 直接调用) ──
    "traefik.http.routers.polystrategy-auth-forward.rule=PathPrefix(`/auth/verify`)",
    "traefik.http.routers.polystrategy-auth-forward.entrypoints=web",
    # ── 声明 forwardAuth 中间件 (供本项目其他服务引用) ──
    f"traefik.http.middlewares.polystrategy-auth.forwardauth.address={AUTH_VERIFY_URL}",
    "traefik.http.middlewares.polystrategy-auth.forwardauth.trustforwardheader=true",
    "traefik.http.middlewares.polystrategy-auth.forwardauth.authresponseheaders=X-User-Id,X-User-Role,X-Username",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_auth_migrations(hash_password)
    logger.info(f"[AuthService] Started on port {SERVICE_PORT}")
    async with consul_lifespan(SERVICE_NAME, SERVICE_PORT, tags=CONSUL_TAGS):
        yield
    logger.info("[AuthService] Stopped")


app = FastAPI(title="Auth Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(forward_auth_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    uvicorn.run("auth_service.app:app", host="0.0.0.0", port=SERVICE_PORT, reload=False)
