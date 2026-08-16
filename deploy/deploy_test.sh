#!/usr/bin/env bash
set -euo pipefail

# WeatherTaker 测试环境部署脚本
# 独立 nginx 实例 + 独立 pid/log，不影响生产

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
BACKEND_PORT="${BACKEND_PORT:-9092}"
NGINX_LISTEN="${NGINX_LISTEN:-8082}"
NGINX_CONF="/etc/nginx/weathertaker-test-nginx.conf"
NGINX_PID="/run/weathertaker-test-nginx.pid"
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
    ENV=test PORT="$BACKEND_PORT" nohup "$APP_DIR/backend/.venv/bin/python" "$APP_DIR/backend/main.py" >> "$STDOUT_LOG" 2>&1 &
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
  sudo install -m 0644 "$APP_DIR/deploy/nginx.conf" "/etc/nginx/weathertaker-test.conf"
  sudo install -m 0644 "$APP_DIR/deploy/weathertaker-test-nginx.conf" "$NGINX_CONF"
  echo "Nginx config installed"
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
    echo "Reloading weathertaker-test nginx master pid=$master_pid"
    sudo kill -HUP "$master_pid"
    return
  fi

  echo "Starting weathertaker-test nginx"
  if [ -f "$NGINX_PID" ] && [ ! -s "$NGINX_PID" ]; then
    sudo rm -f "$NGINX_PID"
  fi
  sudo nginx -c "$NGINX_CONF"
}

# --- Main ---
cd "$APP_DIR"

# Verify .env.prod exists and PORT matches
if [ ! -f backend/.env.test ]; then
  echo "missing backend/.env.test" >&2
  exit 1
fi
if ! grep -q "^PORT=$BACKEND_PORT$" backend/.env.test; then
  echo "backend/.env.test PORT must be $BACKEND_PORT" >&2
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
echo "=== Test environment deployed ==="
echo "Frontend: http://$(hostname -I | awk '{print $1}'):$NGINX_LISTEN"
echo "Backend:  http://127.0.0.1:$BACKEND_PORT"
echo "Logs:     $STDOUT_LOG"
echo ""
tail -20 "$STDOUT_LOG"
