"""MySQL 连接池管理"""
import logging
import os
from contextlib import contextmanager

import pymysql
from dbutils.pooled_db import PooledDB

logger = logging.getLogger(__name__)

_MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "localhost"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", "123456"),
    "database": os.getenv("MYSQL_DATABASE", "weathertaker"),
    "charset": "utf8mb4",
}

_pool = None


def get_db_pool():
    global _pool
    if _pool is None:
        _pool = PooledDB(
            creator=pymysql,
            maxconnections=20,
            mincached=5,
            maxcached=10,
            blocking=True,
            **_MYSQL_CONFIG,
        )
        logger.info("[DB] 连接池初始化完成")
    return _pool


def get_db_connection():
    return get_db_pool().connection()


@contextmanager
def get_db():
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()