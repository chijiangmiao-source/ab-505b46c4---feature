#!/bin/sh
# 守护循环：服务进程退出（含演练用的 /api/admin/shutdown）后自动拉起，
# 使容器保持存活，compose 的 --exit-code-from verify 不会因 app 退出而中止。
trap 'kill "$child" 2>/dev/null; exit 0' TERM INT

PYTHON="$(command -v python3 || command -v python)"

while true; do
  "$PYTHON" server.py &
  child=$!
  wait "$child"
  echo "[supervisor] server exited (code $?), restarting in 0.6s" >&2
  sleep 0.6
done
