#!/usr/bin/env bash
#
# PolyStrategy 基础设施启动脚本（Consul + Traefik）
#
# 用法:
#   ./start-infra.sh          # 启动
#   ./start-infra.sh stop     # 停止
#   ./start-infra.sh status   # 查看状态
#   ./start-infra.sh restart  # 重启
#
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
INFRA_DIR="$ROOT_DIR/infra"
PID_DIR="$ROOT_DIR/.pids"
LOG_DIR="$ROOT_DIR/logs"

mkdir -p "$PID_DIR" "$LOG_DIR/consul" "$LOG_DIR/traefik"

# Consul 数据目录
CONSUL_DATA_DIR="$ROOT_DIR/.consul-data"
mkdir -p "$CONSUL_DATA_DIR"

start_consul() {
    local pid_file="$PID_DIR/consul.pid"
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "  [consul] already running (pid=$(cat "$pid_file"), port=8500)"
        return
    fi

    nohup consul agent \
        -server \
        -bootstrap-expect=1 \
        -ui \
        -client=0.0.0.0 \
        -bind=127.0.0.1 \
        -datacenter=dc1 \
        -data-dir="$CONSUL_DATA_DIR" \
        >> "$LOG_DIR/consul/app.log" 2>&1 &
    echo $! > "$pid_file"

    sleep 1
    if kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "  [consul] started (pid=$(cat "$pid_file"), http=8500, ui=http://localhost:8500)"
    else
        echo "  [consul] FAILED — check logs/consul/app.log"
    fi
}

start_traefik() {
    local pid_file="$PID_DIR/traefik.pid"
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "  [traefik] already running (pid=$(cat "$pid_file"), port=8000)"
        return
    fi

    nohup traefik \
        --configFile="$INFRA_DIR/traefik/traefik.yml" \
        >> "$LOG_DIR/traefik/app.log" 2>&1 &
    echo $! > "$pid_file"

    sleep 1
    if kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "  [traefik] started (pid=$(cat "$pid_file"), entrypoint=8000, dashboard=8080)"
    else
        echo "  [traefik] FAILED — check logs/traefik/app.log"
    fi
}

stop_service() {
    local name="$1"
    local pid_file="$PID_DIR/$name.pid"
    if [ ! -f "$pid_file" ]; then
        echo "  [$name] not running"
        return
    fi
    local pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        sleep 1
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        echo "  [$name] stopped (pid=$pid)"
    else
        echo "  [$name] not running (stale pid)"
    fi
    rm -f "$pid_file"
}

status_service() {
    local name="$1" port="$2"
    local pid_file="$PID_DIR/$name.pid"
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        printf "  %-12s \033[32m● running\033[0m  pid=%-6s port=%s\n" "$name" "$(cat "$pid_file")" "$port"
    else
        printf "  %-12s \033[31m○ stopped\033[0m\n" "$name"
    fi
}

case "${1:-start}" in
    start)
        echo "═══ PolyStrategy Infra — Starting ═══"
        start_consul
        start_traefik
        echo ""
        echo "═══ Infra ready ═══"
        echo "  Consul UI:        http://localhost:8500"
        echo "  Traefik Gateway:  http://localhost:8000"
        echo "  Traefik Dashboard: http://localhost:8080"
        ;;
    stop)
        echo "═══ PolyStrategy Infra — Stopping ═══"
        stop_service "traefik"
        stop_service "consul"
        echo "═══ Infra stopped ═══"
        ;;
    status)
        echo "═══ PolyStrategy Infra — Status ═══"
        status_service "consul" "8500"
        status_service "traefik" "8000"
        ;;
    restart)
        "$0" stop
        sleep 1
        "$0" start
        ;;
    *)
        echo "Usage: $0 {start|stop|status|restart}"
        exit 1
        ;;
esac
