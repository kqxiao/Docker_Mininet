#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import networkx as nx
import sys
import os
import torch
import time

# 引入基础模块
from gnn_lyx import gnn_lyx
import myClass

# ================= 配置 =================
MODEL_PATH = "model_epoch_20000.pth"
TOPO_DIR = "topo_data"
PATH_SAVE_DIR = "topo_path"
INTER_DOMAIN_FILE = "inter_domain_db.json"

HPARAMS = {
    'link_state_dim': 32,
    'path_state_dim': 32,
    'T': 4,
    'readout_units': 16,
    'learn_embedding': True,
    'head_num': 4,
}
DEVICE = torch.device("cpu")
# =======================================


class RouteCalculator:
    def __init__(self):
        self.inter_graph = nx.DiGraph()
        self.intra_graphs = {}
        self.container_topos = {}
        self.inter_links = []

        self.ai_graph_adapter = myClass.m_graph()
        self.model = None

        self._load_model()
        self._prepare_dirs()

    def _prepare_dirs(self):
        if not os.path.exists(PATH_SAVE_DIR):
            os.makedirs(PATH_SAVE_DIR)

    def _load_model(self):
        """加载 PyTorch GNN 模型"""
        print(">>> Loading AI Model...")
        try:
            self.model = gnn_lyx(HPARAMS).to(DEVICE)
            if os.path.exists(MODEL_PATH):
                state_dict = torch.load(
                    MODEL_PATH, map_location=DEVICE, weights_only=True
                )
                self.model.load_state_dict(state_dict)
                self.model.eval()
                print(f"    Model loaded from {MODEL_PATH}")
            else:
                print(f"[Error] Model file {MODEL_PATH} not found!")
                sys.exit(1)
        except Exception as e:
            print(f"[Error] Failed to load AI model: {e}")
            sys.exit(1)

    def load_data(self):
        """加载拓扑数据"""
        print(">>> Loading topology data...")
        inter_path = os.path.join(TOPO_DIR, INTER_DOMAIN_FILE)
        if not os.path.exists(inter_path):
            print(f"Error: {inter_path} not found.")
            sys.exit(1)

        with open(inter_path, "r") as f:
            self.inter_links = json.load(f)

        for filename in os.listdir(TOPO_DIR):
            if filename.endswith("_topo.json"):
                c_name = filename.split("_")[0]
                path = os.path.join(TOPO_DIR, filename)
                with open(path, "r") as f:
                    self.container_topos[c_name] = json.load(f)
        print(f"    Loaded {len(self.container_topos)} domains.")

    def build_graphs(self):
        """构建内存图"""
        print(">>> Building hierarchical graphs...")
        # 1. 域间图
        for link in self.inter_links:
            src_c, dst_c = link['src_container'], link['dst_container']
            bw = link['qos']['bw']
            delay = link['qos'].get('delay', 10)
            self.inter_graph.add_edge(src_c, dst_c, bw=bw, delay=delay, weight=1)
            self.inter_graph.add_edge(dst_c, src_c, bw=bw, delay=delay, weight=1)

        # 2. 域内图
        for c_name, data in self.container_topos.items():
            g = nx.DiGraph()
            # 主机
            for h_name, h_info in data["hosts"].items():
                node_id, sw_name = h_name, h_info["connected_to"]
                g.add_node(node_id, type='host', ip=h_info['ip'], mac=h_info['mac'])
                g.add_edge(
                    node_id,
                    sw_name,
                    weight=1,
                    out_port=f"{h_name}-eth0",
                    bw=10000,
                    delay=0,
                )
                g.add_edge(
                    sw_name,
                    node_id,
                    weight=1,
                    out_port=h_info["port_on_switch"],
                    bw=10000,
                    delay=0,
                )
            # 交换机
            for s_name, links in data["switches"].items():
                if s_name not in g:
                    g.add_node(s_name, type='switch')
                else:
                    g.nodes[s_name]['type'] = 'switch'
                for neighbor, link_info in links.items():
                    if isinstance(link_info, str):
                        real_port, bw = link_info, 1000
                    else:
                        real_port, bw = link_info['port'], link_info.get('bw', 1000)

                    if neighbor.startswith("EXT_"):
                        g.nodes[s_name][f"border_{real_port}"] = True
                    elif neighbor.startswith("s"):
                        g.add_edge(
                            s_name,
                            neighbor,
                            weight=1,
                            out_port=real_port,
                            bw=bw,
                            delay=1,
                        )
            self.intra_graphs[c_name] = g

    # ================= 辅助查找 =================
    def _get_border_switch(self, c_name, eth_name):
        g = self.intra_graphs[c_name]
        target_key = f"border_{eth_name}"
        for node, data in g.nodes(data=True):
            if data.get('type') == 'switch' and data.get(target_key):
                return node
        return None

    def _find_inter_link(self, u_c, v_c):
        for link in self.inter_links:
            if link['src_container'] == u_c and link['dst_container'] == v_c:
                return link
            if link['src_container'] == v_c and link['dst_container'] == u_c:
                return {
                    'src_container': link['dst_container'],
                    'src_iface': link['dst_iface'],
                    'dst_container': link['src_container'],
                    'dst_iface': link['src_iface'],
                    'qos': link['qos'],
                }
        return None

    def _get_domain_interfaces(self, c_name):
        """获取某个域所有的对外接口名 (ethX)"""
        ifaces = []
        for link in self.inter_links:
            if link['src_container'] == c_name:
                ifaces.append(link['src_iface'])
            elif link['dst_container'] == c_name:
                ifaces.append(link['dst_iface'])
        return list(set(ifaces))

    # ================= AI 算路核心 =================

    def get_path_score(self, graph, src, dst, bw_demand):
        """
        计算单条路径的 AI 最高得分 (用于给 '虚边' 赋值)
        """
        # 1. 过滤带宽不足的边
        valid_g = nx.DiGraph()
        for u, v, data in graph.edges(data=True):
            if data['bw'] >= bw_demand:
                valid_g.add_edge(u, v, **data)
            elif u.startswith('h') or v.startswith('h'):
                if not valid_g.has_edge(u, v):
                    valid_g.add_edge(u, v, **data)

        if not nx.has_path(valid_g, src, dst):
            return 0.0

        # 2. AI 推理
        self.ai_graph_adapter.load_from_nx(valid_g, src, dst, bw_demand)
        if len(self.ai_graph_adapter.flows[0].paths) == 0:
            return 0.0

        features = self.ai_graph_adapter.get_features_lyx_batch([0], 0, DEVICE)
        with torch.no_grad():
            q_values = self.model(features)
            best_score = torch.max(q_values).item()

        return best_score

    def get_ai_candidates(self, graph, src, dst, bw_demand, context_name):
        """获取 AI 推荐的路径列表"""
        valid_g = nx.DiGraph()
        for u, v, data in graph.edges(data=True):
            if data['bw'] >= bw_demand:
                valid_g.add_edge(u, v, **data)
            elif (
                u.startswith('h') or v.startswith('h')
            ) and context_name != "Inter-Domain":
                if not valid_g.has_edge(u, v):
                    valid_g.add_edge(u, v, **data)

        if not nx.has_path(valid_g, src, dst):
            return []

        self.ai_graph_adapter.load_from_nx(valid_g, src, dst, bw_demand)
        if len(self.ai_graph_adapter.flows[0].paths) == 0:
            return []

        features = self.ai_graph_adapter.get_features_lyx_batch([0], 0, DEVICE)
        with torch.no_grad():
            q_values = self.model(features)

        sorted_indices = torch.argsort(q_values, descending=True)
        candidates = []
        for idx in sorted_indices:
            idx = idx.item()
            path_ids = self.ai_graph_adapter.flows[0].paths[idx]
            path_names = [self.ai_graph_adapter.id_to_name[pid] for pid in path_ids]
            score = q_values[idx].item()
            candidates.append({'path': path_names, 'score': score})
        return candidates

    def calculate_global_augmented_path(self, src_c, src_h, dst_c, dst_h, bw_demand):
        """
        构建增强图 (Augmented Graph) 并计算最佳域间序列
        """
        print(f"    [Layer 1] Building Augmented Graph (Virtual Edges)...")
        aug_graph = nx.DiGraph()

        # 1. 遍历所有域，构建内部虚边
        all_domains = self.container_topos.keys()

        for domain in all_domains:
            g = self.intra_graphs[domain]
            interfaces = self._get_domain_interfaces(domain)

            iface_to_node = {}
            for iface in interfaces:
                node = self._get_border_switch(domain, iface)
                if node:
                    iface_to_node[iface] = node

            # A. 源域逻辑
            if domain == src_c:
                for iface, node in iface_to_node.items():
                    score = self.get_path_score(g, src_h, node, bw_demand)
                    if score > 0:
                        weight = 1000.0 / score
                        aug_graph.add_edge(
                            "GLOBAL_SRC", f"{domain}:{iface}", weight=weight
                        )

            # B. 目的域逻辑
            if domain == dst_c:
                for iface, node in iface_to_node.items():
                    score = self.get_path_score(g, node, dst_h, bw_demand)
                    if score > 0:
                        weight = 1000.0 / score
                        aug_graph.add_edge(
                            f"{domain}:{iface}", "GLOBAL_DST", weight=weight
                        )

            # C. 过境域逻辑
            import itertools

            for in_iface, out_iface in itertools.permutations(interfaces, 2):
                if in_iface in iface_to_node and out_iface in iface_to_node:
                    u, v = iface_to_node[in_iface], iface_to_node[out_iface]
                    score = self.get_path_score(g, u, v, bw_demand)
                    if score > 0:
                        weight = 1000.0 / score
                        aug_graph.add_edge(
                            f"{domain}:{in_iface}",
                            f"{domain}:{out_iface}",
                            weight=weight,
                        )

        # 2. 构建域间实边
        for link in self.inter_links:
            u = f"{link['src_container']}:{link['src_iface']}"
            v = f"{link['dst_container']}:{link['dst_iface']}"
            if link['qos']['bw'] >= bw_demand:
                aug_graph.add_edge(u, v, weight=0.1)
                aug_graph.add_edge(v, u, weight=0.1)

        # 3. 增强图上跑最短路
        try:
            path_nodes = nx.shortest_path(
                aug_graph, "GLOBAL_SRC", "GLOBAL_DST", weight="weight"
            )

            domain_path = []
            seen_domains = set()
            if src_c not in seen_domains:
                domain_path.append(src_c)
                seen_domains.add(src_c)

            for node in path_nodes:
                if ":" in node:
                    dom = node.split(":")[0]
                    if dom not in seen_domains:
                        domain_path.append(dom)
                        seen_domains.add(dom)

            print(
                f"    [Augmented] Calculated Best Sequence: {' -> '.join(domain_path)}"
            )
            return domain_path

        except nx.NetworkXNoPath:
            print("[Error] No path found in Augmented Graph!")
            return None

    def calculate_and_save(self, src, dst, bw_demand):
        self.load_data()
        self.build_graphs()

        src_c, src_h = src.split(':')
        dst_c, dst_h = dst.split(':')

        print(f"\n>>> Calculating Paths for {src} -> {dst} (BW={bw_demand})...")

        # 1. 域间 AI 算路 (使用增强图)
        domain_path = self.calculate_global_augmented_path(
            src_c, src_h, dst_c, dst_h, bw_demand
        )
        if not domain_path:
            return

        # 2. 域内算路组合
        domain_segments_options = []

        for i in range(len(domain_path)):
            curr_domain = domain_path[i]
            # 确定出入口
            if i == 0:
                ingress = src_h
            else:
                prev = domain_path[i - 1]
                link = self._find_inter_link(prev, curr_domain)
                ingress = self._get_border_switch(curr_domain, link['dst_iface'])

            if i == len(domain_path) - 1:
                egress, egress_iface = dst_h, None
            else:
                next_d = domain_path[i + 1]
                link = self._find_inter_link(curr_domain, next_d)
                egress = self._get_border_switch(curr_domain, link['src_iface'])
                egress_iface = link['src_iface']

            if not ingress or not egress:
                print(f"[Error] Border switch issue in {curr_domain}")
                return

            print(f"    [Domain: {curr_domain}] Calculation {ingress} -> {egress}...")
            candidates = self.get_ai_candidates(
                self.intra_graphs[curr_domain], ingress, egress, bw_demand, curr_domain
            )
            if not candidates:
                return

            # === 打印该域内所有候选路径 ===
            for rank, cand in enumerate(candidates):
                print(
                    f"      [Local Candidate {rank}] Score={cand['score']:.2f} | Path: {' -> '.join(cand['path'])}"
                )

            # 存储候选路径
            domain_options = []
            for cand in candidates:
                domain_options.append(
                    {
                        "container": curr_domain,
                        "path": cand['path'],
                        "score": cand['score'],
                        "egress_iface": egress_iface,
                    }
                )
            domain_segments_options.append(domain_options)

        # 3. 组合生成最终的 Top K 端到端路径
        final_paths = []
        max_k = max([len(opts) for opts in domain_segments_options])

        for k in range(max_k):
            full_path_segments = []
            total_score = 0

            for d_idx, options in enumerate(domain_segments_options):
                idx = min(k, len(options) - 1)
                seg = options[idx]
                full_path_segments.append(seg)
                total_score += seg['score']

            avg_score = total_score / len(domain_path)

            final_paths.append(
                {
                    "rank": k,
                    "score": avg_score,
                    "src_dst": [src, dst],
                    "inter_domain_path": domain_path,
                    "segments": full_path_segments,
                }
            )

            # === 构建完整的路径字符串（使用 dX 缩写）===
            full_path_str_list = []
            for seg in full_path_segments:
                c_name = seg['container']
                # 缩写容器名: docker1 -> d1
                short_name = c_name.replace("docker", "d")

                # 加上容器前缀，例如 d1:s1
                nodes_with_prefix = [f"{short_name}:{node}" for node in seg['path']]
                full_path_str_list.extend(nodes_with_prefix)

            full_path_str = " -> ".join(full_path_str_list)

            print(f"      [Candidate {k}] Score={avg_score:.2f}")
            print(f"        Path: {full_path_str}")

        # 4. 保存到文件
        safe_src = src.replace(':', '_')
        safe_dst = dst.replace(':', '_')
        p1, p2 = sorted([safe_src, safe_dst])
        filename = f"{p1}_{p2}_{int(bw_demand)}.json"

        save_path = os.path.join(PATH_SAVE_DIR, filename)
        with open(save_path, 'w') as f:
            json.dump(final_paths, f, indent=4)

        print(f"\n>>> Calculation Complete. Paths saved to: {save_path}")


if __name__ == "__main__":
    # 记录开始时间
    start_time = time.time()

    if len(sys.argv) < 4:
        print("Usage: sudo python3 route_cal.py <src> <dst> <bw>")
        sys.exit(1)

    src = sys.argv[1]
    dst = sys.argv[2]
    bw = float(sys.argv[3])

    calc = RouteCalculator()
    calc.calculate_and_save(src, dst, bw)

    end_time = time.time()
    print(f"\n[Time] Total Execution Time: {end_time - start_time:.4f} seconds")

