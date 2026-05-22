#!/bin/bash
# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under MIT
#
# 把所有持久化文件 symlink 到 /data，让 host 端只需挂一个 ./data 目录。
# 这样 grid.db / stats.db / logs / runtime_config.json / llm_keys.json /
# prompts/scalp.txt 等数据在容器重建后都不丢。

set -e

DATA="${DATA_DIR:-/data}"
APP="${APP_HOME:-/app}"

mkdir -p "$DATA/logs" "$DATA/prompts"

# 用户编辑过的 prompt 持久化；首次启动从镜像 default 拷贝
if [ ! -f "$DATA/prompts/scalp.txt" ]; then
  cp "$APP/prompts/scalp.default.txt" "$DATA/prompts/scalp.txt"
fi

# 初始化配置/密钥为空对象（让 webui 健康检查通过）
[ -f "$DATA/runtime_config.json" ] || echo '{}' > "$DATA/runtime_config.json"
[ -f "$DATA/llm_keys.json" ]       || echo '{}' > "$DATA/llm_keys.json"
chmod 600 "$DATA/llm_keys.json" 2>/dev/null || true

# 把代码期望的路径 symlink 到 /data
# 注：镜像里 /app 是只读约定，所以先 rm 掉同名文件再 ln
rm -f "$APP/logs" "$APP/grid.db" "$APP/stats.db" \
      "$APP/runtime_config.json" "$APP/llm_keys.json" "$APP/bot.pid" \
      "$APP/prompts/scalp.txt" 2>/dev/null

ln -sf "$DATA/logs"                  "$APP/logs"
ln -sf "$DATA/prompts/scalp.txt"     "$APP/prompts/scalp.txt"
ln -sf "$DATA/runtime_config.json"   "$APP/runtime_config.json"
ln -sf "$DATA/llm_keys.json"         "$APP/llm_keys.json"
# DB 文件由程序首次访问时创建，提前 symlink 占位
ln -sf "$DATA/grid.db"               "$APP/grid.db"
ln -sf "$DATA/stats.db"              "$APP/stats.db"

echo "[entrypoint] TZ=$(date +%Z) data=$DATA"
echo "[entrypoint] starting: $*"
exec "$@"
