#!/usr/bin/env bash
set -euo pipefail

# WeatherTaker 测试环境首次 bootstrap
# 独立 nginx 实例，不影响生产

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
APP_USER="${APP_USER:-$(id -un)}"
SERVER_NAME="${SERVER_NAME:?set SERVER_NAME to your domain or VPS IP}"
BACKEND_PORT="${BACKEND_PORT:-9092}"
NGINX_LISTEN="${NGINX_LISTEN:-8082}"
MIN_PYTHON_VERSION="3.11"
PYTHON_BIN=""

python_satisfies_min_version() {
  "$1" - "$MIN_PYTHON_VERSION" <<'PY'
import sys

minimum = tuple(int(part) for part in sys.argv[1].split("."))
raise SystemExit(0 if sys.version_info[: len(minimum)] >= minimum else 1)
PY
}

find_python() {
  local candidate
  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_satisfies_min_version "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

select_python() {
  if PYTHON_BIN="$(find_python)"; then
    return
  fi
  echo "Python $MIN_PYTHON_VERSION+ is required." >&2
  exit 1
}

ensure_backend_venv() {
  if [ -x backend/.venv/bin/python ]; then
    if python_satisfies_min_version backend/.venv/bin/python; then
      return
    fi
    echo "backend/.venv was created with Python older than $MIN_PYTHON_VERSION; recreating it." >&2
    rm -rf backend/.venv
  fi
  "$PYTHON_BIN" -m venv backend/.venv
}

install_backend_requirements() {
  backend/.venv/bin/python -m pip install --upgrade pip
  (
    cd backend
    .venv/bin/pip install -r requirements.txt
  )
}

select_python
cd "$APP_DIR"

if [ ! -f backend/.env.test ]; then
  echo "missing backend/.env.test; create it before bootstrapping" >&2
  exit 1
fi

if grep -q '^PORT=' backend/.env.test; then
  if ! grep -q "^PORT=$BACKEND_PORT$" backend/.env.test; then
    echo "backend/.env.test PORT must match BACKEND_PORT=$BACKEND_PORT" >&2
    exit 1
  fi
else
  printf '\nPORT=%s\n' "$BACKEND_PORT" >> backend/.env.test
fi

ensure_backend_venv
install_backend_requirements

npm --prefix frontend ci
mkdir -p deploy/state

# 生成 server block 配置
tmp_nginx="$(mktemp)"
sed \
  -e "s#<USER>#$APP_USER#g" \
  -e "s#<SERVER_NAME>#$SERVER_NAME#g" \
  -e "s#listen 8082;#listen $NGINX_LISTEN;#g" \
  -e "s#127.0.0.1:9092#127.0.0.1:$BACKEND_PORT#g" \
  -e "s#/home/$APP_USER/WeatherTaker_testenv/WeatherTaker#$APP_DIR#g" \
  deploy/nginx_test.conf.example > "$tmp_nginx"
sudo install -m 0644 "$tmp_nginx" "/etc/nginx/weathertaker-test.conf"
rm -f "$tmp_nginx"

# 生成独立 nginx 主配置（独立 pid/log，include weathertaker-test.conf）
tmp_nginx_main="$(mktemp)"
cat > "$tmp_nginx_main" <<NGINX_MAIN
user $APP_USER;
worker_processes auto;
error_log /var/log/nginx/weathertaker-test.error.log notice;
pid /run/weathertaker-test-nginx.pid;

include /usr/share/nginx/modules/*.conf;

events {
    worker_connections 1024;
}

http {
    log_format main '\$remote_addr - \$remote_user [\$time_local] "\$request" '
                    '\$status \$body_bytes_sent "\$http_referer" '
                    '"\$http_user_agent" "\$http_x_forwarded_for"';

    access_log /var/log/nginx/weathertaker-test.access.log main;

    sendfile on;
    tcp_nopush on;
    keepalive_timeout 65;
    types_hash_max_size 4096;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    include /etc/nginx/weathertaker-test.conf;
}
NGINX_MAIN
sudo install -m 0644 "$tmp_nginx_main" "/etc/nginx/weathertaker-test-nginx.conf"
rm -f "$tmp_nginx_main"

echo ""
echo "=== Bootstrap done ==="
echo "Nginx configs installed:"
echo "  /etc/nginx/weathertaker-test-nginx.conf (main)"
echo "  /etc/nginx/weathertaker-test.conf (server block)"
echo ""
echo "Run ./deploy/deploy_test.sh to build and start."
