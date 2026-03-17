# 域内网络自动构建脚本
# !/usr/bin/python3
# -*- coding: utf-8 -*-

import sys
import argparse
import subprocess
import json
import time
import re  # 正则模块
import random  # 随机库
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch
from mininet.log import setLogLevel, info
import networkx as nx

# ================= 配置区域 =================
INTRA_PRESETS = {
    "ba_core": {"desc": "BA(20,2) 默认域内拓扑", "type": "ba", "n": 20, "m": 2},
    "ring20": {"desc": "20节点环形", "type": "ring", "n": 20},
    "ws20": {"desc": "WS小世界(20,4,0.25)", "type": "ws", "n": 20, "k": 4, "p": 0.25},
    "er20": {"desc": "ER随机图(20,0.18,保证连通)", "type": "er", "n": 20, "p": 0.18},
    "tree20": {"desc": "20节点随机树", "type": "tree", "n": 20},
}
# ===========================================

def run_cmd(cmd, ignore_error=False):
    try:
        subprocess.check_call(cmd, shell=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError:
        if not ignore_error:
            print(f"Error executing: {cmd}")
            sys.exit(1)


def _build_connected_er_graph(n, p, seed):
    """
    构建连通 ER 图；若不连通则基于 seed 递增重试。
    """
    for offset in range(256):
        s = seed + offset
        G = nx.erdos_renyi_graph(n=n, p=p, seed=s)
        if nx.is_connected(G):
            return G
    raise RuntimeError("Failed to build a connected ER graph after 256 attempts.")


def build_intra_graph(preset_name, seed):
    if preset_name not in INTRA_PRESETS:
        raise ValueError(f"Unknown intra preset: {preset_name}")

    cfg = INTRA_PRESETS[preset_name]
    n = cfg["n"]
    t = cfg["type"]

    if t == "ba":
        return nx.barabasi_albert_graph(n=n, m=cfg["m"], seed=seed)
    if t == "ring":
        return nx.cycle_graph(n)
    if t == "ws":
        return nx.connected_watts_strogatz_graph(
            n=n, k=cfg["k"], p=cfg["p"], seed=seed
        )
    if t == "er":
        return _build_connected_er_graph(n=n, p=cfg["p"], seed=seed)
    if t == "tree":
        return nx.random_labeled_tree(n=n, seed=seed)

    raise ValueError(f"Unsupported intra topology type: {t}")


def get_external_interfaces():
    """
    获取容器内真正的外部接口 (eth0, eth1...)
    过滤掉 lo, ovs-system 以及 Mininet 生成的 sX-ethY 接口
    """
    interfaces = []
    try:
        output = subprocess.check_output("ip link show", shell=True).decode('utf-8')
        for line in output.split('\n'):
            if line.strip() and not line.startswith(' '):
                # 提取接口名 (如 "29: eth0@if30: ...")
                parts = line.split(':')
                if len(parts) >= 2:
                    iface_name = parts[1].strip().split('@')[0]

                    # === 使用正则严格匹配 eth + 数字 ===
                    # 只有像 eth0, eth1, eth10 这样的名字才会被加入
                    # s1-eth1, lo, ovs-system 统统会被过滤掉
                    if re.match(r'^eth\d+$', iface_name):
                        interfaces.append(iface_name)

    except Exception as e:
        info(f"*** Error getting interfaces: {e}\n")

    # 排序，确保 eth0 在 eth1 前面
    interfaces.sort(key=lambda x: int(x.replace("eth", "")))
    return interfaces


def attach_external_links(net, G, nx_node_to_switch):
    """
    将容器外部接口挂载到 Mininet 核心交换机
    返回: external_map { 'sX': ['eth0', 'eth1'] } 用于记录拓扑
    """
    info('\n*** 正在检测外部接口并挂载到核心交换机...\n')
    ext_ifaces = get_external_interfaces()
    external_map = {}

    if not ext_ifaces:
        info('*** 未检测到外部接口 (ethX)，跳过挂载。\n')
        return external_map

    degrees = sorted(G.degree(), key=lambda x: x[1], reverse=True)
    info(f'*** 检测到外部接口: {ext_ifaces}\n')

    for i, iface in enumerate(ext_ifaces):
        node_id, degree = degrees[i % len(degrees)]
        target_switch = nx_node_to_switch[node_id]
        sw_name = target_switch.name

        info(f'    [绑定] {iface} <---> {sw_name} (Degree: {degree})\n')

        run_cmd(f"ip link set {iface} up")
        run_cmd(f"ovs-vsctl add-port {sw_name} {iface}")

        # 记录映射关系，方便保存拓扑
        if sw_name not in external_map:
            external_map[sw_name] = []
        external_map[sw_name].append(iface)

    return external_map


def save_topology(net, docker_id, external_map, qos_data):
    """
    保存详细的拓扑信息 (含端口号和QoS信息) 到 JSON
    qos_data: {(u, v): {'bw': 100}} 键为排序后的节点ID元组

    JSON 结构示例:
    topo_data = {
        "docker_id": docker_id,
        "switches": {},  # s1: { "s2": {...}, "EXTERNAL": ["eth0"] }
        "hosts": {}      # h1: { "ip":..., "mac":..., "connected_to": "s1", "port_on_switch": "s1-eth2" }
    }
    """
    info('*** Saving topology to /root/topo_db.json...\n')

    topo_data = {"docker_id": docker_id, "switches": {}, "hosts": {}}

    # 1. 记录交换机互联
    for link in net.links:
        n1, n2 = link.intf1.node, link.intf2.node
        p1_name, p2_name = link.intf1.name, link.intf2.name

        # 仅处理 Switch-Switch 连接
        if 's' in n1.name and 's' in n2.name:
            if n1.name not in topo_data["switches"]:
                topo_data["switches"][n1.name] = {}
            if n2.name not in topo_data["switches"]:
                topo_data["switches"][n2.name] = {}

            # 提取数字ID用于查找QoS数据
            id1 = int(n1.name[1:]) - 1  # s1 -> 0
            id2 = int(n2.name[1:]) - 1  # s2 -> 1
            key = tuple(sorted((id1, id2)))

            # 获取QoS，如果找不到则用默认值
            qos = qos_data.get(key, {'bw': 1000})

            # 存入字典结构
            topo_data["switches"][n1.name][n2.name] = {"port": p1_name, "bw": qos['bw']}
            topo_data["switches"][n2.name][n1.name] = {"port": p2_name, "bw": qos['bw']}

    # 2. 记录外部接口 (从 attach_external_links 返回的 map 获取)
    for sw_name, ifaces in external_map.items():
        if sw_name not in topo_data["switches"]:
            topo_data["switches"][sw_name] = {}
        # 记录外部接口列表
        for iface in ifaces:
            # 外部接口给予默认的高性能参数 (模拟光纤互联)
            topo_data["switches"][sw_name][f"EXT_{iface}"] = {
                "port": iface,
                "bw": 1000,  # 1 Gbps
            }

    # 3. 记录主机及其连接
    for h in net.hosts:
        # 获取主机连接的链路对象
        link = h.intfList()[0].link
        # 确定哪一头是交换机
        if link.intf1.node == h:
            sw_intf = link.intf2
            sw_node = link.intf2.node
        else:
            sw_intf = link.intf1
            sw_node = link.intf1.node

        topo_data["hosts"][h.name] = {
            "ip": h.IP(),
            "mac": h.MAC(),
            "connected_to": sw_node.name,
            "port_on_switch": sw_intf.name,  # 交换机端连接主机的端口名
        }

        # 同时也把 Host 连接记在 Switch 记录里，方便反查
        if sw_node.name not in topo_data["switches"]:
            topo_data["switches"][sw_node.name] = {}

        # 主机连接也给予默认参数 (接入层)
        topo_data["switches"][sw_node.name][h.name] = {"port": sw_intf.name, "bw": 1000}

    with open('/root/topo_db.json', 'w') as f:
        json.dump(topo_data, f, indent=4)


def build_switch_topo(seed, docker_id, preset_name="ba_core"):
    if preset_name not in INTRA_PRESETS:
        raise ValueError(f"Unknown intra preset: {preset_name}")

    cfg = INTRA_PRESETS[preset_name]
    info(
        f"*** 正在初始化 Mininet (Preset={preset_name}, Seed={seed}, DockerID={docker_id})\n"
    )

    # 随机数种子设置，保证每次生成的QoS参数一致但随机
    random.seed(seed)

    net = Mininet(controller=None, switch=OVSKernelSwitch)

    # 按预设生成域内拓扑图
    G = build_intra_graph(preset_name, seed)
    info(f"*** 域内预设: {preset_name} ({cfg['desc']})\n")

    nx_node_to_switch = {}
    for i in G.nodes():
        idx = i + 1
        sw_name = f's{idx}'
        switch = net.addSwitch(sw_name)
        nx_node_to_switch[i] = switch

        # === 挂载主机 ===
        host_name = f'h{idx}'
        host_ip = f'10.0.{docker_id}.{idx}/24'
        host_mac = f'00:00:00:00:{docker_id:02x}:{idx:02x}'

        info(
            f'    [添加主机] {host_name} -> {sw_name} (IP={host_ip}, MAC={host_mac})\n'
        )
        host = net.addHost(host_name, ip=host_ip, mac=host_mac)
        net.addLink(host, switch)

    # === 生成并记录链路QoS参数 ===
    qos_data = {}
    # 添加交换机链路
    for u, v in G.edges():
        net.addLink(nx_node_to_switch[u], nx_node_to_switch[v])

        # 随机生成参数
        # 带宽: 30Mbps ~ 50Mbps
        bw = random.randint(30, 50)

        # 使用排序后的元组作为键，保证无向边的唯一性
        key = tuple(sorted((u, v)))
        qos_data[key] = {'bw': bw}

        info(f"    [Link QoS] s{u+1}-s{v+1}: BW={bw}Mbps\n")

    info('*** Starting network\n')
    net.start()

    # === 挂载外部接口并获取映射 ===
    ext_map = attach_external_links(net, G, nx_node_to_switch)

    # === 保存拓扑 ===
    # 传入 qos_data 进行保存
    save_topology(net, docker_id, ext_map, qos_data)

    info('*** Network is running in BACKGROUND.\n')
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        net.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--id', type=int, help='Docker Container ID')
    parser.add_argument(
        '--preset',
        default='ba_core',
        help='Intra-domain topology preset'
    )
    parser.add_argument(
        '--list-presets',
        action='store_true',
        help='List all intra-domain presets'
    )
    args = parser.parse_args()

    if args.list_presets:
        print("Available intra-domain presets:")
        for name in sorted(INTRA_PRESETS.keys()):
            print(f"  - {name}: {INTRA_PRESETS[name]['desc']}")
        sys.exit(0)
    if args.id is None:
        parser.error("the following arguments are required: --id")

    setLogLevel('info')
    build_switch_topo(args.seed, args.id, args.preset)
