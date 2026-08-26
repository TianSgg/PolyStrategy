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

from auth_service.api import router as auth_router
from auth_service.migrations import run_auth_migrations
from auth_service.forward_auth import router as forward_auth_router
from framework.consul import consul_lifespan

logger = logging.getLogger(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "auth-service")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8010"))

CONSUL_TAGS = [
    "traefik.enable=true",
    "traefik.http.routers.auth.rule=PathPrefix(`/api/auth`)",
    "traefik.http.routers.auth.entrypoints=web",
    "traefik.http.routers.auth-forward.rule=PathPrefix(`/auth/verify`)",
    "traefik.http.routers.auth-forward.entrypoints=web",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_auth_migrations()
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
