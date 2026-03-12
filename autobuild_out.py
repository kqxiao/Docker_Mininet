# 域间网络自动化构建脚本
# !/usr/bin/env python3
# -*- coding: utf-8 -*-

import networkx as nx
import subprocess
import sys
import json
import random  # 引入随机库

# ================= 配置区域 =================
NUM_NODES = 5  # 节点数量
BA_M = 2  # BA模型新节点连接数
BRIDGE_NAME = "br-sdn"
DOCKER_IMAGE = "mininet_docker"
# ===========================================


def run_cmd(cmd, ignore_error=False):
    try:
        subprocess.check_call(cmd, shell=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError:
        if not ignore_error:
            print(f"Error executing: {cmd}")
            sys.exit(1)


def main():
    print(f"*** 1. 生成 BA 模型拓扑 (N={NUM_NODES}, m={BA_M})...")
    # 设置随机种子保证拓扑一致，但在边生成后再随机QoS
    G = nx.barabasi_albert_graph(n=NUM_NODES, m=BA_M, seed=1)

    # 为了让QoS参数每次运行稍有不同（或者你可以固定seed保证完全复现）
    random.seed(1)

    print("\n*** 2. 清理环境...")
    run_cmd(
        f"docker rm -f $(docker ps -a -q --filter name=docker) 2>/dev/null",
        ignore_error=True,
    )
    run_cmd(f"sudo ovs-vsctl del-br {BRIDGE_NAME}", ignore_error=True)

    print(f"\n*** 3. 重建网桥 {BRIDGE_NAME}...")
    run_cmd(f"sudo ovs-vsctl add-br {BRIDGE_NAME}")
    run_cmd(f"sudo ip link set {BRIDGE_NAME} up")
    run_cmd(f"sudo ovs-ofctl del-flows {BRIDGE_NAME}")

    print("\n*** 4. 启动 Docker 容器...")
    node_eth_counter = {i: 0 for i in range(NUM_NODES)}
    node_to_docker = {i: f"docker{i+1}" for i in range(NUM_NODES)}

    for i in range(NUM_NODES):
        c_name = node_to_docker[i]
        run_cmd(
            f"sudo docker run -itd --rm --privileged --name {c_name} --net=none {DOCKER_IMAGE} /bin/bash"
        )
    print("    容器启动完成。")

    print("\n*** 5. 顺序添加端口并配置流表...")
    current_ofport = 1
    inter_domain_links = []  # 用于保存拓扑数据

    for u, v in G.edges():
        c_u = node_to_docker[u]
        c_v = node_to_docker[v]

        eth_u = f"eth{node_eth_counter[u]}"
        eth_v = f"eth{node_eth_counter[v]}"

        node_eth_counter[u] += 1
        node_eth_counter[v] += 1

        # === 随机生成 QoS 参数 (仅带宽) ===
        # 带宽: 100Mbps ~ 200Mbps (域间通常比域内快)
        bw = random.randint(100, 200)

        # 记录拓扑关系及QoS
        inter_domain_links.append(
            {
                "src_container": c_u,
                "src_iface": eth_u,
                "dst_container": c_v,
                "dst_iface": eth_v,
                "qos": {"bw": bw},
            }
        )

        # 添加端口
        port_u = current_ofport
        run_cmd(f"sudo ovs-docker add-port {BRIDGE_NAME} {eth_u} {c_u}")
        current_ofport += 1

        port_v = current_ofport
        run_cmd(f"sudo ovs-docker add-port {BRIDGE_NAME} {eth_v} {c_v}")
        current_ofport += 1

        print(
            f"    Link: {c_u}:{eth_u}(Port {port_u}) <--> {c_v}:{eth_v}(Port {port_v}) [BW={bw}Mbps]"
        )

        # 下发底层静态路由 (直连)
        run_cmd(
            f"sudo ovs-ofctl add-flow {BRIDGE_NAME} \"priority=150,in_port={port_u},actions=output:{port_v}\""
        )
        run_cmd(
            f"sudo ovs-ofctl add-flow {BRIDGE_NAME} \"priority=150,in_port={port_v},actions=output:{port_u}\""
        )

    # === 保存域间拓扑 ===
    print("\n*** 6. 保存域间拓扑数据...")
    with open("inter_domain_db.json", "w") as f:
        json.dump(inter_domain_links, f, indent=4)
    print("    Saved to inter_domain_db.json")

    print("\n*** 完成！")


if __name__ == "__main__":
    main()

