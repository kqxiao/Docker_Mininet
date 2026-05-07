#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import networkx as nx
import sys
import os
import torch
import time
import argparse

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


def _normalize_num(value):
    value_f = float(value)
    if value_f.is_integer():
        return str(int(value_f))
    return f"{value_f:.3f}".rstrip("0").rstrip(".")


def build_path_filename(src, dst, bw, max_delay=None, max_loss=None):
    safe_src = src.replace(':', '_')
    safe_dst = dst.replace(':', '_')
    p1, p2 = sorted([safe_src, safe_dst])
    suffix = _normalize_num(bw)
    if max_delay is not None:
        suffix += f"_d{_normalize_num(max_delay)}"
    if max_loss is not None:
        suffix += f"_l{_normalize_num(max_loss)}"
    return f"{p1}_{p2}_{suffix}.json"


def combine_loss_pct(current_loss, link_loss):
    """按独立链路丢包率合成端到端丢包率，单位都是百分比。"""
    current = min(max(float(current_loss), 0.0), 100.0) / 100.0
    link = min(max(float(link_loss), 0.0), 100.0) / 100.0
    return (1.0 - (1.0 - current) * (1.0 - link)) * 100.0


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
        print(">>> Loading AI Model...", flush=True)
        try:
            self.model = gnn_lyx(HPARAMS).to(DEVICE)
            if os.path.exists(MODEL_PATH):
                state_dict = torch.load(
                    MODEL_PATH, map_location=DEVICE, weights_only=True
                )
                self.model.load_state_dict(state_dict)
                self.model.eval()
                print(f"    Model loaded from {MODEL_PATH}", flush=True)
            else:
                print(f"[Error] Model file {MODEL_PATH} not found!")
                sys.exit(1)
        except Exception as e:
            print(f"[Error] Failed to load AI model: {e}")
            sys.exit(1)

    def load_data(self):
        """加载拓扑数据"""
        print(">>> Loading topology data...", flush=True)
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
        print(f"    Loaded {len(self.container_topos)} domains.", flush=True)

    def build_graphs(self):
        """构建内存图"""
        print(">>> Building hierarchical graphs...", flush=True)
        # 1. 域间图
        for link in self.inter_links:
            src_c, dst_c = link['src_container'], link['dst_container']
            qos = link.get('qos', {})
            bw = qos.get('bw', 0)
            delay = qos.get('delay', 10)
            loss = qos.get('loss', 0.0)
            self.inter_graph.add_edge(
                src_c, dst_c, bw=bw, delay=delay, loss=loss, weight=1
            )
            self.inter_graph.add_edge(
                dst_c, src_c, bw=bw, delay=delay, loss=loss, weight=1
            )

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
                    loss=0.0,
                )
                g.add_edge(
                    sw_name,
                    node_id,
                    weight=1,
                    out_port=h_info["port_on_switch"],
                    bw=10000,
                    delay=0,
                    loss=0.0,
                )
            # 交换机
            for s_name, links in data["switches"].items():
                if s_name not in g:
                    g.add_node(s_name, type='switch')
                else:
                    g.nodes[s_name]['type'] = 'switch'
                for neighbor, link_info in links.items():
                    if isinstance(link_info, str):
                        real_port, bw, delay, loss = link_info, 1000, 1, 0.0
                    else:
                        real_port = link_info['port']
                        bw = link_info.get('bw', 1000)
                        delay = link_info.get('delay', 1)
                        loss = link_info.get('loss', 0.0)

                    if neighbor.startswith("EXT_"):
                        g.nodes[s_name][f"border_{real_port}"] = True
                    elif neighbor.startswith("s"):
                        g.add_edge(
                            s_name,
                            neighbor,
                            weight=1,
                            out_port=real_port,
                            bw=bw,
                            delay=delay,
                            loss=loss,
                        )
            self.intra_graphs[c_name] = g

    def _path_metrics(self, graph, path):
        metrics = {"bw": float("inf"), "delay": 0.0, "loss": 0.0}
        for i in range(len(path) - 1):
            u, v = path[i], path[i + 1]
            if not graph.has_edge(u, v):
                continue
            data = graph[u][v]
            metrics["bw"] = min(metrics["bw"], float(data.get("bw", 0)))
            metrics["delay"] += float(data.get("delay", 0))
            metrics["loss"] = combine_loss_pct(metrics["loss"], data.get("loss", 0.0))
        if metrics["bw"] == float("inf"):
            metrics["bw"] = 0.0
        return metrics

    def _inter_path_metrics(self, inter_path):
        metrics = {"bw": float("inf"), "delay": 0.0, "loss": 0.0}
        for i in range(len(inter_path) - 1):
            link = self._find_inter_link(inter_path[i], inter_path[i + 1])
            if not link:
                continue
            qos = link.get("qos", {})
            metrics["bw"] = min(metrics["bw"], float(qos.get("bw", 0)))
            metrics["delay"] += float(qos.get("delay", 10))
            metrics["loss"] = combine_loss_pct(metrics["loss"], qos.get("loss", 0.0))
        if metrics["bw"] == float("inf"):
            metrics["bw"] = 0.0
        return metrics

    def _end_to_end_metrics(self, segments, inter_path):
        metrics = self._inter_path_metrics(inter_path)
        for seg in segments:
            c_name = seg.get("container")
            g = self.intra_graphs.get(c_name)
            if not g:
                continue
            seg_metrics = self._path_metrics(g, seg.get("path", []))
            if seg_metrics["bw"] > 0:
                metrics["bw"] = min(metrics["bw"], seg_metrics["bw"])
            metrics["delay"] += seg_metrics["delay"]
            metrics["loss"] = combine_loss_pct(metrics["loss"], seg_metrics["loss"])
        if metrics["bw"] == float("inf"):
            metrics["bw"] = 0.0
        return metrics

    def _metrics_satisfy(self, metrics, bw_demand, max_delay=None, max_loss=None):
        if metrics.get("bw", 0) < bw_demand:
            return False
        if max_delay is not None and metrics.get("delay", 0) > max_delay:
            return False
        if max_loss is not None and metrics.get("loss", 0) > max_loss:
            return False
        return True

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
            candidates.append(
                {
                    'path': path_names,
                    'score': score,
                    'metrics': self._path_metrics(valid_g, path_names),
                }
            )
        return candidates

    def calculate_global_augmented_paths(self, src_c, src_h, dst_c, dst_h, bw_demand):
        """
        构建增强图 (Augmented Graph) 并计算若干候选域间序列
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
                qos = link.get('qos', {})
                edge_weight = (
                    0.1
                    + float(qos.get('delay', 10)) * 0.01
                    + float(qos.get('loss', 0.0)) * 0.2
                )
                aug_graph.add_edge(u, v, weight=edge_weight)
                aug_graph.add_edge(v, u, weight=edge_weight)

        # 3. 增强图上跑 K 条短路，提取不重复的域序列
        try:
            path_iter = nx.shortest_simple_paths(
                aug_graph, "GLOBAL_SRC", "GLOBAL_DST", weight="weight"
            )
            domain_paths = []
            seen_paths = set()
            for path_nodes in path_iter:
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

                key = tuple(domain_path)
                if key in seen_paths:
                    continue
                seen_paths.add(key)
                domain_paths.append(domain_path)
                print(
                    f"    [Augmented] Candidate Domain Sequence: {' -> '.join(domain_path)}"
                )
                if len(domain_paths) >= 5:
                    break

            if not domain_paths:
                print("[Error] No path found in Augmented Graph!")
                return []
            return domain_paths

        except (nx.NetworkXNoPath, nx.NodeNotFound):
            print("[Error] No path found in Augmented Graph!")
            return []

    def calculate_and_save(self, src, dst, bw_demand, max_delay=None, max_loss=None):
        self.load_data()
        self.build_graphs()

        src_c, src_h = src.split(':')
        dst_c, dst_h = dst.split(':')

        print(
            f"\n>>> Calculating Paths for {src} -> {dst} "
            f"(BW={bw_demand}Mbps, MaxDelay={max_delay if max_delay is not None else 'N/A'}ms, "
            f"MaxLoss={max_loss if max_loss is not None else 'N/A'}%)...",
            flush=True,
        )
        # 1. 域间 AI 算路 (使用增强图)
        domain_paths = self.calculate_global_augmented_paths(
            src_c, src_h, dst_c, dst_h, bw_demand
        )
        if not domain_paths:
            return

        # 2. 域内算路组合
        final_paths = []

        for domain_path in domain_paths:
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
                    domain_segments_options = []
                    break

                print(f"    [Domain: {curr_domain}] Calculation {ingress} -> {egress}...")
                candidates = self.get_ai_candidates(
                    self.intra_graphs[curr_domain], ingress, egress, bw_demand, curr_domain
                )
                if not candidates:
                    domain_segments_options = []
                    break

                # === 打印该域内所有候选路径 ===
                for rank, cand in enumerate(candidates):
                    m = cand.get('metrics', {})
                    print(
                        f"      [Local Candidate {rank}] Score={cand['score']:.2f} "
                        f"| Delay={m.get('delay', 0):.2f}ms Loss={m.get('loss', 0):.3f}% "
                        f"| Path: {' -> '.join(cand['path'])}"
                    )

                # 存储候选路径
                domain_options = []
                for cand in candidates:
                    domain_options.append(
                        {
                            "container": curr_domain,
                            "path": cand['path'],
                            "score": cand['score'],
                            "metrics": cand.get('metrics', {}),
                            "egress_iface": egress_iface,
                        }
                    )
                domain_segments_options.append(domain_options)

            if not domain_segments_options:
                continue

            # 3. 组合生成最终的 Top K 端到端路径
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
                metrics = self._end_to_end_metrics(full_path_segments, domain_path)
                if not self._metrics_satisfy(metrics, bw_demand, max_delay, max_loss):
                    print(
                        f"      [Skip Candidate] Delay={metrics['delay']:.2f}ms "
                        f"Loss={metrics['loss']:.3f}% does not satisfy constraints."
                    )
                    continue

                final_paths.append(
                    {
                        "rank": len(final_paths),
                        "score": avg_score,
                        "src_dst": [src, dst],
                        "qos": {
                            "bw": float(bw_demand),
                            "max_delay": max_delay,
                            "max_loss": max_loss,
                        },
                        "metrics": metrics,
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

                print(f"      [Candidate {len(final_paths)-1}] Score={avg_score:.2f}")
                print(
                    f"        Metrics: BW={metrics['bw']:.2f}Mbps, "
                    f"Delay={metrics['delay']:.2f}ms, Loss={metrics['loss']:.3f}%"
                )
                print(f"        Path: {full_path_str}")

                if len(final_paths) >= 5:
                    break
            if len(final_paths) >= 5:
                break

        if not final_paths:
            print("[Error] No path satisfies requested QoS constraints.")
            return

        # 4. 保存到文件
        filename = build_path_filename(src, dst, bw_demand, max_delay, max_loss)

        save_path = os.path.join(PATH_SAVE_DIR, filename)
        with open(save_path, 'w') as f:
            json.dump(final_paths, f, indent=4)

        print(f"\n>>> Calculation Complete. Paths saved to: {save_path}")


if __name__ == "__main__":
    # 记录开始时间
    start_time = time.time()

    parser = argparse.ArgumentParser()
    parser.add_argument("src", help="Source node (e.g. docker1:h1)")
    parser.add_argument("dst", help="Destination node (e.g. docker5:h1)")
    parser.add_argument("bw", type=float, help="Bandwidth demand in Mbps")
    parser.add_argument(
        "max_delay",
        type=float,
        nargs="?",
        default=None,
        help="Optional end-to-end max delay in ms",
    )
    parser.add_argument(
        "max_loss",
        type=float,
        nargs="?",
        default=None,
        help="Optional end-to-end max packet loss in percent",
    )
    args = parser.parse_args()

    calc = RouteCalculator()
    calc.calculate_and_save(
        args.src,
        args.dst,
        args.bw,
        max_delay=args.max_delay,
        max_loss=args.max_loss,
    )

    end_time = time.time()
    print(f"\n[Time] Total Execution Time: {end_time - start_time:.4f} seconds")
