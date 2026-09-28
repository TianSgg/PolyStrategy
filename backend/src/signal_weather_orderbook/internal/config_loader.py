"""服务配置加载器：读取各服务目录下的 config.yml"""
import os
from pathlib import Path

import yaml


def load_service_config(service_dir: str = None) -> dict:
    if service_dir is None:
        service_dir = Path(__file__).parent
    config_dir = Path(service_dir)
    config_path = config_dir / "config.yml"
    if not config_path.exists():
        config_path = config_dir / "config.example.yml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    _apply_env_overrides(config)
    return config


def _apply_env_overrides(config: dict) -> None:
    """环境变量覆盖 config.yml 中的值（用于敏感信息）"""
    if "service" in config:
        svc = config["service"]
        if port := os.getenv("SERVICE_PORT"):
            svc["port"] = int(port)

    if "strategy" in config:
        strat = config["strategy"]
        if wallet := os.getenv("PROXY_WALLET"):
            strat["proxy_wallet"] = wallet
        if key := os.getenv("PRIVATE_KEY"):
            strat["private_key"] = key
