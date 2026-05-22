#!/usr/bin/env bash
# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License.
#
# OrbitAI helper script — 管理 webui 进程
# Usage:
#   ./bot.sh setup     # 创建 .venv + 装依赖（首次用）
#   ./bot.sh start     # 后台启动 webui
#   ./bot.sh stop      # 停止 webui（不影响已挂的 OKX 订单）
#   ./bot.sh restart   # 重启
#   ./bot.sh status    # 状态
#   ./bot.sh logs      # tail -f webui 日志
#   ./bot.sh run       # 前台启动（开发用，Ctrl+C 退出）

set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8765}"
PY=".venv/bin/python"
WEBUI_LOG="logs/webui.log"

# 颜色
RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[1;33m'; CYN=$'\033[0;36m'; NC=$'\033[0m'

_check_env() {
  if [ ! -f .env ]; then
    echo "${RED}❌ 找不到 .env，请先 cp .env.example .env 并填入凭证${NC}"
    exit 1
  fi
  if [ ! -x "$PY" ]; then
    echo "${RED}❌ 找不到 .venv（请先跑 ./bot.sh setup）${NC}"
    exit 1
  fi
}

_load_env() {
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
}

_pid() {
  # 优先 lsof（macOS 默认；多数发行版需 yum/apt install lsof）
  # 没装 lsof 时退化到 ss / fuser（Linux 通常自带）
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti:"$PORT" 2>/dev/null | head -1
  elif command -v ss >/dev/null 2>&1; then
    ss -lntp "sport = :$PORT" 2>/dev/null \
      | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2
  elif command -v fuser >/dev/null 2>&1; then
    fuser "$PORT/tcp" 2>/dev/null | awk '{print $1}'
  fi
}

cmd_setup() {
  echo "${CYN}▶ 创建 .venv${NC}"
  if [ ! -d .venv ]; then
    python3 -m venv .venv
  fi
  echo "${CYN}▶ 安装 orbitai 包（editable 模式）${NC}"
  ./.venv/bin/pip install -e .
  if [ ! -f .env ]; then
    cp .env.example .env
    echo "${YLW}⚠ 已创建 .env，请编辑后填入凭证${NC}"
  fi
  echo "${GRN}✅ 环境就绪。下一步：编辑 .env，然后 ./bot.sh start${NC}"
}

cmd_start() {
  _check_env
  if [ -n "$(_pid)" ]; then
    echo "${YLW}已在运行 (PID $(_pid))${NC}"
    return 0
  fi
  mkdir -p logs
  _load_env
  echo "${CYN}▶ 启动 webui http://$HOST:$PORT${NC}"
  nohup "$PY" -m uvicorn orbitai.web.app:app --host "$HOST" --port "$PORT" \
    >> "$WEBUI_LOG" 2>&1 &
  disown
  # 等就绪
  for _ in $(seq 1 20); do
    if [ -n "$(_pid)" ]; then
      echo "${GRN}✅ 已启动 (PID $(_pid))${NC}"
      echo "  日志: tail -f $WEBUI_LOG"
      return 0
    fi
    sleep 0.3
  done
  echo "${RED}❌ 启动失败，看 $WEBUI_LOG${NC}"
  tail -20 "$WEBUI_LOG" 2>/dev/null || true
  exit 1
}

cmd_stop() {
  local pid
  pid="$(_pid)"
  if [ -z "$pid" ]; then
    echo "${YLW}未在运行${NC}"
    return 0
  fi
  echo "${CYN}▶ 停止 webui (PID $pid)${NC}"
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 30); do
    [ -z "$(_pid)" ] && { echo "${GRN}✅ 已停止${NC}"; return 0; }
    sleep 0.3
  done
  echo "${YLW}优雅退出超时，强杀${NC}"
  kill -KILL "$pid" 2>/dev/null || true
  echo "${GRN}✅ 已停止${NC}"
}

cmd_restart() {
  cmd_stop || true
  sleep 1
  cmd_start
}

cmd_status() {
  local pid
  pid="$(_pid)"
  if [ -n "$pid" ]; then
    echo "${GRN}● webui 运行中${NC}  PID=$pid  URL=http://$HOST:$PORT"
    if [ -f bot.pid ]; then
      bp=$(cat bot.pid)
      if kill -0 "$bp" 2>/dev/null; then
        echo "${GRN}● bot   运行中${NC}  PID=$bp"
      else
        echo "${YLW}● bot   未运行${NC}  (pid 文件残留)"
      fi
    else
      echo "${YLW}● bot   未运行${NC}"
    fi
  else
    echo "${RED}● webui 未运行${NC}"
  fi
}

cmd_logs() {
  if [ ! -f "$WEBUI_LOG" ]; then
    echo "${YLW}尚无日志文件 $WEBUI_LOG${NC}"
    exit 0
  fi
  tail -f -n 100 "$WEBUI_LOG"
}

cmd_run() {
  _check_env
  _load_env
  echo "${CYN}▶ 前台启动 webui http://$HOST:$PORT  (Ctrl+C 退出)${NC}"
  exec "$PY" -m uvicorn orbitai.web.app:app --host "$HOST" --port "$PORT"
}

cmd_help() {
  cat <<EOF
OrbitAI helper

Usage: ./bot.sh <command>

Commands:
  setup     创建 .venv 并安装依赖
  start     后台启动 webui
  stop      停止 webui
  restart   重启 webui
  status    查看运行状态
  logs      实时跟随日志
  run       前台启动（开发用）
  help      显示本帮助

Environment overrides:
  PORT=8765 HOST=0.0.0.0
EOF
}

case "${1:-help}" in
  setup)   cmd_setup ;;
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  run)     cmd_run ;;
  help|-h|--help) cmd_help ;;
  *) echo "${RED}未知命令: $1${NC}"; cmd_help; exit 1 ;;
esac
