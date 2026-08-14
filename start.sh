#!/bin/bash
# WeatherTaker 启动脚本
# 用法: ./start.sh

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"
PYTHON="/Users/liutianshi/anaconda3/envs/weathertaker/bin/python"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}=== WeatherTaker Startup ===${NC}"

# 检查 Python
if [ ! -x "$PYTHON" ]; then
    echo -e "${RED}Error: Python not found at $PYTHON${NC}"
    echo "Create conda env: conda create -n weathertaker python=3.10"
    exit 1
fi

PY_VERSION=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo -e "${GREEN}Python $PY_VERSION OK${NC}"

# 检查 .env.dev
if [ ! -f "$BACKEND_DIR/.env.dev" ]; then
    echo -e "${RED}Error: backend/.env.dev not found${NC}"
    echo "Copy from .env.example and fill in your config"
    exit 1
fi

# 检查 node_modules
if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    echo -e "${YELLOW}Installing frontend dependencies...${NC}"
    cd "$FRONTEND_DIR" && npm install
fi

# 启动后端
echo -e "${GREEN}Starting backend (port 8000)...${NC}"
cd "$BACKEND_DIR"
$PYTHON main.py &
BACKEND_PID=$!

# 等待后端启动
sleep 3
if ! kill -0 $BACKEND_PID 2>/dev/null; then
    echo -e "${RED}Backend failed to start. Check logs above.${NC}"
    exit 1
fi

# 启动前端 (exec npx vite 让 vite 成为直接子进程，避免 npm 父进程提前退出)
echo -e "${GREEN}Starting frontend (port 5173)...${NC}"
cd "$FRONTEND_DIR"
npx vite &
FRONTEND_PID=$!

echo ""
echo -e "${GREEN}=== Running ===${NC}"
echo -e "  Backend:  http://localhost:8000"
echo -e "  Frontend: http://localhost:5173"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop both${NC}"

# 捕获退出信号，关闭两个进程
cleanup() {
    echo ""
    echo -e "${YELLOW}Shutting down...${NC}"
    kill $FRONTEND_PID 2>/dev/null
    kill $BACKEND_PID 2>/dev/null
    wait $FRONTEND_PID 2>/dev/null
    wait $BACKEND_PID 2>/dev/null
    echo -e "${GREEN}Done.${NC}"
}
trap cleanup SIGINT SIGTERM

# 等待后端进程（主进程），Ctrl+C 会触发 cleanup
wait $BACKEND_PID 2>/dev/null
cleanup
