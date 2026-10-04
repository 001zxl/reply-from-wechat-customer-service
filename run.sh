#!/usr/bin/env bash
# 启动网点客服助手。
#
# 密钥优先级： 环境变量 DEEPSEEK_API_KEY  >  .env  >  ~/.dsh/.credentials.yaml
# （DSH 凭据只是在内存里读一下做兜底，不会写进任何文件）
#
# 用法：
#   ./run.sh              # 启动服务（打印 APP_TOKEN，审核台右上角填它）
#   ./run.sh sim          # 跑离线回归（16 项，不花钱、不联网）
#   ./run.sh live         # 跑真实模型契约测试（会计费）
#   ./run.sh biz          # 跑业务场景测试：18 个真实网点场景
#   ./run.sh talk         # 跑多轮对话模拟：连发/改主意/带情绪/超范围
#   ./run.sh models       # 看有哪些模型、切换、体检
#   ./run.sh guard ...    # 会话白名单：list-chats / allow-chat / audit
#   ./run.sh live --case 4
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "找不到 .venv，请先执行："
  echo "  export UV_CACHE_DIR=\"\$(pwd)/../.uv-cache\""
  echo "  uv venv --python 3.12 .venv"
  echo "  uv pip install --python .venv/bin/python -r requirements.txt --index-url https://pypi.tuna.tsinghua.edu.cn/simple"
  exit 1
fi

# 从文本文件里取一个 KEY=VALUE 的值（去引号、去空白）
read_env_file() {   # $1=文件  $2=键名
  [ -f "$1" ] || return 0
  sed -n "s/^[[:space:]]*$2=[[:space:]]*//p" "$1" | head -1 | tr -d '"' | tr -d "'" | tr -d '[:space:]'
}

ENV_KEY="$(read_env_file .env DEEPSEEK_API_KEY)"
if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -z "$ENV_KEY" ] && [ -f "$HOME/.dsh/.credentials.yaml" ]; then
  # 兜底：从 DSH 凭据里取（yaml 格式，注意有缩进）
  DEEPSEEK_API_KEY=$(sed -n 's/^[[:space:]]*DEEPSEEK_API_KEY:[[:space:]]*//p' "$HOME/.dsh/.credentials.yaml" \
                     | head -1 | tr -d '"' | tr -d "'" | tr -d '[:space:]')
  export DEEPSEEK_API_KEY
fi

if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -z "$ENV_KEY" ]; then
  echo "警告：没有找到 DEEPSEEK_API_KEY（环境变量和 .env 都没有），真实模型调用会失败。"
fi

# APP_TOKEN：环境变量 > .env > 随机生成
APP_TOKEN="${APP_TOKEN:-$(read_env_file .env APP_TOKEN)}"
if [ -z "$APP_TOKEN" ]; then
  APP_TOKEN="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(24))')"
fi
export APP_TOKEN
echo "APP_TOKEN = $APP_TOKEN   （审核台右上角填这个）"

case "${1:-serve}" in
  sim)  exec "$PY" tests/simulate.py ;;
  live) shift; exec "$PY" tests/run_cases.py "$@" ;;
  biz)  shift; exec "$PY" tests/run_business.py "$@" ;;
  talk) shift; exec "$PY" tests/run_conversation.py "$@" ;;
  models) shift; exec "$PY" bridge/switch_model.py "$@" ;;
  guard) shift; exec "$PY" bridge/guard.py "$@" ;;
  serve|*)
    exec "$PY" -m uvicorn app.server:app \
      --host "${HOST:-127.0.0.1}" --port "${PORT:-8787}" \
      --log-level "${LOG_LEVEL:-info}"
    ;;
esac
