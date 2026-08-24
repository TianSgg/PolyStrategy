的哥#!/usr/bin/env bash
#
# PolyStrategy 全栈启动脚本
#
# 用法:
#   ./start.sh              # 启动全部（后端 + 前端）
#   ./start.sh stop         # 停止全部
#   ./start.sh status       # 查看状态
#   ./start.sh restart      # 重启全部
#
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"

PID_DIR="$ROOT_DIR/.pids"
mkdir -p "$PID_DIR"

export ENV="${ENV:-dev}"

# 使用当前环境的 Python（确保先 conda activate 正确环境）
PYTHON="$(which python)"
echo "Using Python: $PYTHON"

# ─── 服务定义 ────────────────────────────────────────────────────────────────
# 后端服务统一用 python -m 方式运行，工作目录为 backend/src，避免 types.py 遮蔽标准库

BACKEND_SERVICES=(
    # 名称|模块|端口|额外环境变量
    "signal_weather|signal_weather_orderbook.app|8001|"
    "signal_leader|signal_leader_activity.app|8002|"
    "strategy_sweep|strategy_weather_sweep.app|8003|"
    "strategy_leader|strategy_leader.app|8004|"
    "strategy_sweep_leader|strategy_sweep_leader.app|8005|"
    "gateway|main_gateway|8000|"
)

# ─── 工具函数 ────────────────────────────────────────────────────────────────

start_backend_service() {
    local name="$1" module="$2" port="$3" extra_env="$4"
    local pid_file="$PID_DIR/$name.pid"

    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "  [$name] already running (pid=$(cat "$pid_file"), port=$port)"
        return
    fi

    local cmd
    if [ "$module" = "main_gateway" ]; then
        cmd="cd '$BACKEND_DIR' && PYTHONPATH='$BACKEND_DIR/src' $PYTHON main.py"
    else
        cmd="cd '$BACKEND_DIR/src' && $extra_env PYTHONPATH='$BACKEND_DIR/src' $PYTHON -m $module"
    fi

    # 服务自行写日志到 logs/<service_name>/，这里只捕获启动失败的 stderr
    bash -c "nohup bash -c \"$cmd\" > /dev/null 2>&1 & echo \$! > '$pid_file'"

    sleep 0.3
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "  [$name] started (pid=$(cat "$pid_file"), port=$port)"
    else
        echo "  [$name] FAILED — check logs/$name/"
    fi
}

start_frontend() {
    local pid_file="$PID_DIR/frontend.pid"
    local log_dir="$ROOT_DIR/logs/frontend"
    mkdir -p "$log_dir"

    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "  [frontend] already running (pid=$(cat "$pid_file"), port=5173)"
        return
    fi

    bash -c "cd '$FRONTEND_DIR' && nohup npx vite --port 5173 --host >> '$log_dir/app.log' 2>&1 & echo \$! > '$pid_file'"

    sleep 0.5
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "  [frontend] started (pid=$(cat "$pid_file"), port=5173)"
    else
        echo "  [frontend] FAILED — check logs/frontend/"
    fi
}

stop_service() {
    local name="$1"
    local pid_file="$PID_DIR/$name.pid"

    if [ ! -f "$pid_file" ]; then
        echo "  [$name] not running"
        return
    fi

    local pid
    pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 10); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.3
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        echo "  [$name] stopped (pid=$pid)"
    else
        echo "  [$name] already dead (stale pid=$pid)"
    fi
    rm -f "$pid_file"
}

status_service() {
    local name="$1" port="$2"
    local pid_file="$PID_DIR/$name.pid"

    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        printf "  %-24s \033[32m● running\033[0m  pid=%-6s port=%s\n" "$name" "$(cat "$pid_file")" "$port"
    else
        printf "  %-24s \033[31m○ stopped\033[0m\n" "$name"
    fi
}

# ─── 入口 ────────────────────────────────────────────────────────────────────

case "${1:-start}" in
    start)
        echo "═══ PolyStrategy — Starting all services ═══"
        echo ""
        echo "Signal services:"
        for svc in "${BACKEND_SERVICES[@]}"; do
            IFS='|' read -r name module port extra_env <<< "$svc"
            [[ "$name" == signal_* ]] && start_backend_service "$name" "$module" "$port" "$extra_env"
        done
        sleep 1

        echo ""
        echo "Strategy services:"
        for svc in "${BACKEND_SERVICES[@]}"; do
            IFS='|' read -r name module port extra_env <<< "$svc"
            [[ "$name" == strategy_* ]] && start_backend_service "$name" "$module" "$port" "$extra_env"
        done

        echo ""
        echo "Gateway:"
        for svc in "${BACKEND_SERVICES[@]}"; do
            IFS='|' read -r name module port extra_env <<< "$svc"
            [[ "$name" == "gateway" ]] && start_backend_service "$name" "$module" "$port" "$extra_env"
        done

        echo ""
        echo "Frontend:"
        start_frontend

        echo ""
        echo "═══ All services started ═══"
        echo "  Frontend:  http://localhost:5173"
        echo "  Gateway:   http://localhost:8000"
        echo "  Logs:      $ROOT_DIR/logs/<service_name>/"
        ;;
    stop)
        echo "═══ PolyStrategy — Stopping all services ═══"
        for svc in "${BACKEND_SERVICES[@]}"; do
            IFS='|' read -r name _ _ _ <<< "$svc"
            stop_service "$name"
        done
        stop_service "frontend"
        echo "═══ All services stopped ═══"
        ;;
    status)
        echo "═══ PolyStrategy — Service Status ═══"
        echo ""
        for svc in "${BACKEND_SERVICES[@]}"; do
            IFS='|' read -r name _ port _ <<< "$svc"
            status_service "$name" "$port"
        done
        status_service "frontend" "5173"
        echo ""
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
