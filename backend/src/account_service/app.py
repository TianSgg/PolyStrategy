"""Account Service — 账户管理微服务。

职责：
- 账户 CRUD（/api/account/*）
- Market 价格查询（/api/market/*）
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

from account.api import router as account_router
from market.api import router as market_router
from shared.consul import consul_lifespan

logger = logging.getLogger(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "account-service")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8011"))

CONSUL_TAGS = [
    "traefik.enable=true",
    "traefik.http.routers.account.rule=PathPrefix(`/api/account`) || PathPrefix(`/api/market`)",
    "traefik.http.routers.account.entrypoints=web",
    "traefik.http.routers.account.middlewares=forward-auth@file",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"[AccountService] Started on port {SERVICE_PORT}")
    async with consul_lifespan(SERVICE_NAME, SERVICE_PORT, tags=CONSUL_TAGS):
        yield
    logger.info("[AccountService] Stopped")


app = FastAPI(title="Account Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(account_router)
app.include_router(market_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    uvicorn.run("account_service.app:app", host="0.0.0.0", port=SERVICE_PORT, reload=False)
