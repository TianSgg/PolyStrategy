#!/usr/bin/env bash
set -euo pipefail

# WeatherTaker 生产环境部署脚本
# 独立 nginx 实例，一键拉起前后端

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
APP_USER="${APP_USER:-$(id -un)}"
BACKEND_PORT="${BACKEND_PORT:-9091}"
NGINX_LISTEN="${NGINX_LISTEN:-8081}"
NGINX_CONF="/etc/nginx/weathertaker-nginx.conf"
NGINX_SERVER_CONF="/etc/nginx/weathertaker.conf"
NGINX_PID="/run/weathertaker-nginx.pid"
STATE_DIR="$APP_DIR/deploy/state"
PID_FILE="$STATE_DIR/backend.pid"
STDOUT_LOG="$STATE_DIR/backend.out.log"

is_backend_running() {
  local pid="$1"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" >/dev/null 2>&1 || return 1
  ps -p "$pid" -o command= | grep -F "$APP_DIR/backend/main.py" >/dev/null 2>&1
}

stop_backend() {
  local pid=""
  if [ -f "$PID_FILE" ]; then
    pid="$(cat "$PID_FILE")"
  fi

  if is_backend_running "$pid"; then
    echo "Stopping backend pid=$pid"
    kill "$pid"
    for _ in $(seq 1 20); do
      if ! is_backend_running "$pid"; then
        break
      fi
      sleep 1
    done
    if is_backend_running "$pid"; then
      echo "Backend did not stop in time; killing pid=$pid"
      kill -9 "$pid"
    fi
  fi

  rm -f "$PID_FILE"
}

start_backend() {
  mkdir -p "$STATE_DIR"
  : >> "$STDOUT_LOG"
  (
    cd "$APP_DIR/backend"
    ENV=prod PORT="$BACKEND_PORT" nohup "$APP_DIR/backend/.venv/bin/python" "$APP_DIR/backend/main.py" >> "$STDOUT_LOG" 2>&1 &
    echo $! > "$PID_FILE"
  )

  local pid
  pid="$(cat "$PID_FILE")"
  sleep 2
  if ! is_backend_running "$pid"; then
    echo "Backend failed to start. Last logs:" >&2
    tail -80 "$STDOUT_LOG" >&2
    exit 1
  fi
  echo "Backend started pid=$pid port=$BACKEND_PORT"
}

install_nginx() {
  # 从模板生成 server block
  local tmp_server="$(mktemp)"
  sed \
    -e "s#<USER>#$APP_USER#g" \
    -e "s#<SERVER_NAME>#$(hostname -I | awk '{print $1}')#g" \
    -e "s#listen 8081;#listen $NGINX_LISTEN;#g" \
    -e "s#127.0.0.1:9091#127.0.0.1:$BACKEND_PORT#g" \
    -e "s#/home/$APP_USER/weathertaker#$APP_DIR#g" \
    "$APP_DIR/deploy/nginx_prod.conf.example" > "$tmp_server"
  sudo install -m 0644 "$tmp_server" "$NGINX_SERVER_CONF"
  rm -f "$tmp_server"

  # 生成 nginx 主配置
  local tmp_main="$(mktemp)"
  cat > "$tmp_main" <<NGINX_MAIN
user $APP_USER;
worker_processes auto;
error_log /var/log/nginx/weathertaker.error.log notice;
pid $NGINX_PID;

include /usr/share/nginx/modules/*.conf;

events {
    worker_connections 1024;
}

http {
    log_format main '\$remote_addr - \$remote_user [\$time_local] "\$request" '
                    '\$status \$body_bytes_sent "\$http_referer" '
                    '"\$http_user_agent" "\$http_x_forwarded_for"';

    access_log /var/log/nginx/weathertaker.access.log main;

    sendfile on;
    tcp_nopush on;
    keepalive_timeout 65;
    types_hash_max_size 4096;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    include $NGINX_SERVER_CONF;
}
NGINX_MAIN
  sudo install -m 0644 "$tmp_main" "$NGINX_CONF"
  rm -f "$tmp_main"
  echo "Nginx configs installed"
}

reload_or_start_nginx() {
  if ! sudo nginx -t -c "$NGINX_CONF" 2>&1; then
    echo "Nginx config test failed" >&2
    exit 1
  fi

  local master_pid=""
  if [ -s "$NGINX_PID" ]; then
    master_pid="$(cat "$NGINX_PID")"
  fi
  if [ -n "$master_pid" ] && kill -0 "$master_pid" 2>/dev/null; then
    echo "Reloading weathertaker nginx master pid=$master_pid"
    sudo kill -HUP "$master_pid"
    return
  fi

  echo "Starting weathertaker nginx"
  if [ -f "$NGINX_PID" ] && [ ! -s "$NGINX_PID" ]; then
    sudo rm -f "$NGINX_PID"
  fi
  sudo nginx -c "$NGINX_CONF"
}

# --- Main ---
cd "$APP_DIR"

if [ ! -f backend/.env.prod ]; then
  echo "missing backend/.env.prod" >&2
  exit 1
fi
if ! grep -q "^PORT=$BACKEND_PORT$" backend/.env.prod; then
  echo "backend/.env.prod PORT must be $BACKEND_PORT" >&2
  exit 1
fi

# Install deps if needed
if [ ! -x backend/.venv/bin/python ]; then
  python3.11 -m venv backend/.venv
fi
(cd backend && .venv/bin/pip install -q -r requirements.txt)

# Build frontend
npm --prefix frontend ci --silent
VITE_API_BASE="" VITE_WS_BASE="" npm --prefix frontend run build

# Deploy
stop_backend
start_backend
install_nginx
reload_or_start_nginx

echo ""
echo "=== Production deployed ==="
echo "Frontend: http://$(hostname -I | awk '{print $1}'):$NGINX_LISTEN"
echo "Backend:  http://127.0.0.1:$BACKEND_PORT"
echo "Logs:     $STDOUT_LOG"
echo ""
tail -20 "$STDOUT_LOG"
