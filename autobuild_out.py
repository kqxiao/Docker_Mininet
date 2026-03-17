# 域间网络自动化构建脚本
# !/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import networkx as nx
import subprocess
import sys
import json
import random  # 引入随机库

# ================= 配置区域 =================
BRIDGE_NAME = "br-sdn"
DOCKER_IMAGE = "mininet_docker"
INTER_PRESETS = {
    "ba_core": {"desc": "BA(5,2) 默认域间拓扑", "type": "ba", "n": 5, "m": 2},
    "ring5": {"desc": "5节点环形", "type": "ring", "n": 5},
    "star5": {"desc": "5节点星形(0为中心)", "type": "star", "n": 5},
    "mesh5": {"desc": "5节点全互联", "type": "complete", "n": 5},
}
# ===========================================


def run_cmd(cmd, ignore_error=False):
    try:
        subprocess.check_call(cmd, shell=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError:
        if not ignore_error:
            print(f"Error executing: {cmd}")
            sys.exit(1)


def build_inter_graph(preset_name, seed):
    if preset_name not in INTER_PRESETS:
        raise ValueError(f"Unknown inter preset: {preset_name}")
    cfg = INTER_PRESETS[preset_name]
    n = cfg["n"]
    t = cfg["type"]

    if t == "ba":
        G = nx.barabasi_albert_graph(n=n, m=cfg["m"], seed=seed)
    elif t == "ring":
        G = nx.cycle_graph(n)
    elif t == "star":
        G = nx.star_graph(n - 1)
    elif t == "complete":
        G = nx.complete_graph(n)
    else:
        raise ValueError(f"Unsupported inter topology type: {t}")
    return G


def main(preset_name="ba_core", seed=1):
    if preset_name not in INTER_PRESETS:
        print(f"[Error] 未知域间拓扑预设: {preset_name}")
        print("可选预设:", ", ".join(sorted(INTER_PRESETS.keys())))
        sys.exit(1)

    cfg = INTER_PRESETS[preset_name]
    print(f"*** 1. 生成域间拓扑: {preset_name} ({cfg['desc']})")
    G = build_inter_graph(preset_name, seed)

    # QoS随机种子可控
    random.seed(seed)

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
    node_eth_counter = {i: 0 for i in range(cfg["n"])}
    node_to_docker = {i: f"docker{i+1}" for i in range(cfg["n"])}

    for i in range(cfg["n"]):
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", default="ba_core", help="Inter-domain topology preset")
    parser.add_argument("--seed", type=int, default=1, help="Random seed for QoS/topology")
    parser.add_argument("--list-presets", action="store_true", help="List all inter-domain presets")
    args = parser.parse_args()

    if args.list_presets:
        print("Available inter-domain presets:")
        for name in sorted(INTER_PRESETS.keys()):
            print(f"  - {name}: {INTER_PRESETS[name]['desc']}")
        sys.exit(0)

    main(preset_name=args.preset, seed=args.seed)
