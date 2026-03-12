# myClass.py (适配 NetworkX 版)
from collections import deque
import numpy as np
import math
import heapq
import copy
import torch
import networkx as nx


class m_node:
    def __init__(self, id, name):
        self.id = id
        self.name = name  # 记录真实名字，如 "docker1:s1"


class m_edge:
    def __init__(self, u, v, next_head, next_tail, bw, delay):
        self.u = u
        self.v = v
        self.next_head = next_head
        self.next_tail = next_tail
        self.bw = bw
        self.delay = delay


class m_flow:
    def __init__(self, s, t, bw):
        self.s = s
        self.t = t
        self.bw = bw
        self.paths = []


class m_ksp_node1:
    def __init__(self, v, d):
        self.v = v
        self.d = d

    def __lt__(self, other):
        return self.d < other.d


class m_ksp_node2:
    def __init__(self, v, d, H, last_path: list):
        self.v = v
        self.d = d
        self.H = H
        self.path = copy.deepcopy(last_path)
        self.path.append(v)

    def __lt__(self, other):
        return self.d + self.H[self.v] < other.d + self.H[other.v]


class m_graph:
    K_SP_CNT = 5  # 候选路径数量，通常 5 条够选了

    def __init__(self):
        self.reset()

    def reset(self):
        self.n = 0
        self.m = 0
        self.f = 0
        self.nodes = []
        self.edges = []
        self.edge_head = []
        self.edge_tail = []
        self.flows = []
        self.H = []
        self.jieshu_ret = []
        self.name_to_id = {}  # 映射: "docker1:s1" -> 0
        self.id_to_name = {}  # 映射: 0 -> "docker1:s1"

    def add_edge_internal(self, u, v, bw, delay):
        # 内部添加边的方法
        self.edges.append(m_edge(u, v, self.edge_head[u], self.edge_tail[v], bw, delay))
        self.edge_head[u] = len(self.edges) - 1
        self.edge_tail[v] = len(self.edges) - 1

    def load_from_nx(self, G, req_src, req_dst, req_bw):
        """
        核心适配函数：从 NetworkX 图加载数据
        G: NetworkX Graph (包含所有节点和边，带属性)
        req_src: 源节点名 (str)
        req_dst: 目的节点名 (str)
        req_bw: 业务带宽需求
        """
        self.reset()
        self.n = len(G.nodes)
        self.m = len(G.edges)

        # 1. 建立节点映射
        for idx, node in enumerate(G.nodes()):
            self.nodes.append(m_node(idx, node))
            self.name_to_id[node] = idx
            self.id_to_name[idx] = node

        self.edge_head = [-1 for _ in range(self.n)]
        self.edge_tail = [-1 for _ in range(self.n)]
        self.H = [float('inf') for _ in range(self.n)]

        # 2. 导入边 (视为有向边)
        for u_name, v_name, data in G.edges(data=True):
            u = self.name_to_id[u_name]
            v = self.name_to_id[v_name]

            # 获取属性，如果没有则给默认值
            bw = data.get('bw', 1000)
            delay = data.get('delay', 1)

            # 添加边 u->v
            self.add_edge_internal(u, v, bw, delay)
            # 因为是无向图逻辑的NetworkX转双向有向边，如果G已经是DiGraph且包含反向边则不需要手动加
            # 这里假设输入 G 是 DiGraph，且已经包含了双向连接

        # 3. 设置业务流 (Flow)
        if req_src in self.name_to_id and req_dst in self.name_to_id:
            s_id = self.name_to_id[req_src]
            t_id = self.name_to_id[req_dst]
            self.flows.append(m_flow(s_id, t_id, req_bw))
            self.f = 1
        else:
            print(f"[Error] Source {req_src} or Dest {req_dst} not in graph!")

        # 4. 预计算 (介数等) - 简化版，为了速度可以跳过介数计算，或者给默认值
        # 这里为了配合模型输入，我们需要填充 self.jieshu_ret
        # 这是一个耗时操作 O(N^3)，如果节点多建议优化。这里先给全0或者简单计算
        self.jieshu_ret = [0.0] * self.m

        # 5. 计算候选路径
        if self.f > 0:
            self.cal_k_sp(0)         

    def cal_k_sp(self, flow_id):
        s = self.flows[flow_id].s
        t = self.flows[flow_id].t

        # 第一遍dijkstra (反向计算 H 值)
        heap1 = [m_ksp_node1(t, 0)]
        vis = [False for _ in range(self.n)]

        # 重置 H
        self.H = [float('inf')] * self.n

        while heap1:
            node_now = heapq.heappop(heap1)
            u, d = node_now.v, node_now.d
            if vis[u]:
                continue
            vis[u] = True
            self.H[u] = d

            # 反向遍历边 (u是终点，找谁连向u)
            e_id = self.edge_tail[u]
            while e_id != -1:
                edge = self.edges[e_id]
                # edge.u -> edge.v (u). 距离累加
                if not vis[edge.u]:
                    heapq.heappush(heap1, m_ksp_node1(edge.u, d + edge.delay))
                e_id = edge.next_tail

        # 第二遍dijkstra (A* 搜索 K 短路)
        heap2 = [m_ksp_node2(s, 0, self.H, [])]
        cnt = [0 for _ in range(self.n)]

        while heap2:
            node_now = heapq.heappop(heap2)
            u, d, path = node_now.v, node_now.d, node_now.path

            cnt[u] += 1
            if u == t:
                path.append(u)  # 补上终点
                self.flows[flow_id].paths.append(copy.deepcopy(path))
                if cnt[u] >= self.K_SP_CNT:
                    break  # 找够了

            if cnt[u] > self.K_SP_CNT:
                continue

            e_id = self.edge_head[u]
            while e_id != -1:
                edge = self.edges[e_id]
                # 简单防环
                if edge.v not in path:
                    heapq.heappush(
                        heap2, m_ksp_node2(edge.v, d + edge.delay, self.H, path)
                    )
                e_id = edge.next_head

    # --- 特征提取相关函数 ---
    def get_edgeId_by_node(self, u, v):
        e_id = self.edge_head[u]
        while e_id != -1:
            edge = self.edges[e_id]
            if edge.v == v:
                return e_id
            e_id = edge.next_head
        return -1

    def get_link_capacity(self):
        return [edge.bw for edge in self.edges]

    def get_link_capacity_available(self, now_actions):
        link_capacity = [edge.bw for edge in self.edges]
        # 这是一个单业务场景，now_actions通常是 [path_index]
        for i in range(self.f):
            if not self.flows[i].paths:
                continue
            # 确保 action 索引有效
            act = now_actions[i] if i < len(now_actions) else 0
            if act >= len(self.flows[i].paths):
                act = 0

            path = self.flows[i].paths[act]
            for j in range(len(path) - 1):
                eid = self.get_edgeId_by_node(path[j], path[j + 1])
                if eid != -1:
                    link_capacity[eid] -= self.flows[i].bw
        return link_capacity

    def get_link_delay(self):
        return [edge.delay for edge in self.edges]

    def get_path_attr(self):
        return [[flow.bw] for flow in self.flows]

    def get_mask(self, now_actions):
        mask = [[False for _ in range(self.m)] for _ in range(self.f)]
        for i in range(self.f):
            if not self.flows[i].paths:
                continue
            act = now_actions[i] if i < len(now_actions) else 0
            if act >= len(self.flows[i].paths):
                act = 0

            path = self.flows[i].paths[act]
            for node_id in range(len(path) - 1):
                link_id = self.get_edgeId_by_node(path[node_id], path[node_id + 1])
                if link_id != -1:
                    mask[i][link_id] = True
        return mask

    def get_link_attr(self, now_actions):
        link_capacity = self.get_link_capacity()
        link_capacity_available = self.get_link_capacity_available(now_actions)
        link_delay = self.get_link_delay()
        link_betweenness = self.jieshu_ret

        link_attr = [
            [
                link_capacity[i],
                link_capacity_available[i],
                link_delay[i],
                link_betweenness[i],
            ]
            for i in range(self.m)
        ]
        return link_attr

    def get_features_lyx_batch(self, now_actions, now_judge_flow_id, device):
        # 适配 GNN 的 Batch 输入构造
        link_attr_list = []
        path_attr_list = []
        mask_list = []

        now_actions_copy = copy.deepcopy(now_actions)

        flow = self.flows[now_judge_flow_id]
        # 看看有多少条候选路径
        num_candidates = len(flow.paths)
        if num_candidates == 0:
            return None

        # 为每一条候选路径构造一个图状态
        for i in range(num_candidates):
            now_actions_copy[now_judge_flow_id] = i
            link_attr_list.append(self.get_link_attr(now_actions_copy))
            path_attr_list.append(self.get_path_attr())
            mask_list.append(self.get_mask(now_actions_copy))

        features = {
            'link_attr': torch.tensor(
                link_attr_list, dtype=torch.float32, device=device
            ),
            'path_attr': torch.tensor(
                path_attr_list, dtype=torch.float32, device=device
            ),
            'mask': torch.tensor(mask_list, dtype=torch.bool, device=device),
        }
        return features

