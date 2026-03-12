#!/bin/bash
if [ -z "$1" ]; then
  echo "Usage: m <host> <command...>"
  exit 1
fi

NAME=$1
shift

# 查找 Mininet 主机 PID (兼容 docker ps 输出格式)
PID=$(ps -ef | grep "mininet:$NAME" | grep -v grep | awk '{print $2}' | head -n 1)

if [ -z "$PID" ]; then
  echo "Error: Host '$NAME' not found. Is the Mininet CLI running?"
  exit 1
fi

# 执行 mnexec
mnexec -a $PID "$@"
