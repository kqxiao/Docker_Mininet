#!/bin/bash

# 1. 启动 Open vSwitch 服务
# Docker 容器中不会自动启动 Systemd 服务，所以需要手动运行
echo "[Entrypoint] Starting Open vSwitch service..."
service openvswitch-switch start

# 2. 检查 OVS 是否启动成功
# 如果没有使用 --privileged 模式运行容器，这里通常会失败
if ovs-vsctl show > /dev/null 2>&1; then
    echo "[Entrypoint] Open vSwitch started successfully."
else
    echo "[Entrypoint] WARNING: Open vSwitch failed to start."
    echo "[Entrypoint] Please ensure you are running this container with '--privileged'."
fi

# 3. 清理 Mininet 残留 (可选但推荐)
# 清理之前可能遗留的虚拟网络接口或进程
echo "[Entrypoint] Cleaning up Mininet (mn -c)..."
mn -c > /dev/null 2>&1

# 4. 执行传入的命令
# "$@" 代表 Dockerfile 中的 CMD (通常是 /bin/bash)
# 使用 exec 可以让该进程替代当前脚本进程，成为 PID 1
echo "[Entrypoint] Executing command: $@"
exec "$@"
