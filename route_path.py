#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import subprocess
import sys
import os
import argparse
import time
import copy

# ================= 配置 =================
TOPO_DIR = "topo_data"
PATH_SAVE_DIR = "topo_path"
INTER_DOMAIN_FILE = "inter_domain_db.json"
ACTIVE_FLOW_FILE = os.path.join(TOPO_DIR, "active_flows.json")
# =======================================


def run_cmd(cmd):
    """执行命令并打印，自动忽略 'File exists' 错误"""
    try:
        subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        output = e.output.decode()
        if "File exists" in output:
            pass
        else:
            print(f"[Error] Command failed: {cmd}")
            print(f"       Output: {output.strip()}")


class RouteDeployer:
    def __init__(self):
        self.container_topos = {}
        self.inter_links = []
        self.active_flows = {}
        self.load_current_topology()
        self._load_active_flows()

    def load_current_topology(self):
        """加载最新的网络状态"""
        inter_path = os.path.join(TOPO_DIR, INTER_DOMAIN_FILE)
        if not os.path.exists(inter_path):
            print(f"[Error] {inter_path} not found.")
            sys.exit(1)
        with open(inter_path, "r") as f:
            self.inter_links = json.load(f)

        for filename in os.listdir(TOPO_DIR):
            if filename.endswith("_topo.json"):
                c_name = filename.split("_")[0]
                path = os.path.join(TOPO_DIR, filename)
                with open(path, "r") as f:
                    self.container_topos[c_name] = json.load(f)

    def _normalize_bw_str(self, bw):
        bw_f = float(bw)
        if bw_f.is_integer():
            return str(int(bw_f))
        return f"{bw_f:.3f}".rstrip("0").rstrip(".")

    def _flow_key(self, src, dst, bw):
        p1, p2 = sorted([src, dst])
        return f"{p1}|{p2}|{self._normalize_bw_str(bw)}"

    def _load_active_flows(self):
        if not os.path.exists(ACTIVE_FLOW_FILE):
            self.active_flows = {}
            return
        try:
            with open(ACTIVE_FLOW_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.active_flows = {
                    item["flow_key"]: item
                    for item in data
                    if isinstance(item, dict) and "flow_key" in item
                }
            elif isinstance(data, dict):
                self.active_flows = data
            else:
                self.active_flows = {}
        except Exception as e:
            print(f"[Warning] Failed to load active flow db: {e}")
            self.active_flows = {}

    def _save_active_flows(self):
        data = [self.active_flows[k] for k in sorted(self.active_flows.keys())]
        with open(ACTIVE_FLOW_FILE, "w") as f:
            json.dump(data, f, indent=4)

    def _save_inter_links(self):
        with open(os.path.join(TOPO_DIR, INTER_DOMAIN_FILE), "w") as f:
            json.dump(self.inter_links, f, indent=4)

    def _save_container_topo(self, c_name):
        with open(os.path.join(TOPO_DIR, f"{c_name}_topo.json"), "w") as f:
            json.dump(self.container_topos[c_name], f, indent=4)

    def _is_container_running(self, container_name):
        try:
            cmd = f"sudo docker inspect -f '{{{{.State.Running}}}}' {container_name}"
            output = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT)
            return output.decode().strip().lower() == "true"
        except subprocess.CalledProcessError:
            return False

    def _run_docker_batch(self, c_name, cmd_list):
        """
        在同一个 docker exec 中批量执行命令，减少进程启动开销。
        """
        if not cmd_list:
            return
        script = "; ".join(cmd_list)
        # 单引号转义，确保 sh -c 可执行
        escaped = script.replace("'", "'\"'\"'")
        run_cmd(f"sudo docker exec {c_name} sh -c '{escaped}'")

    def _collect_switches_from_segments(self, segments):
        container_switches = {}
        for seg in segments:
            c_name = seg.get('container')
            if c_name not in self.container_topos:
                continue
            for node in seg.get('path', []):
                if isinstance(node, str) and node.startswith('s'):
                    container_switches.setdefault(c_name, set()).add(node)
        return container_switches

    # ================= 辅助函数 =================

    def _find_inter_link_idx(self, u_c, v_c):
        for i, link in enumerate(self.inter_links):
            if (link['src_container'] == u_c and link['dst_container'] == v_c) or (
                link['src_container'] == v_c and link['dst_container'] == u_c
            ):
                return i
        return -1

    def _get_inter_link_exit(self, curr_domain, next_domain):
        """查找从 curr_domain 到 next_domain 的出口网卡"""
        for link in self.inter_links:
            if (
                link['src_container'] == curr_domain
                and link['dst_container'] == next_domain
            ):
                return link['src_iface']
            if (
                link['dst_container'] == curr_domain
                and link['src_container'] == next_domain
            ):
                return link['dst_iface']
        return None

    def invert_segments(self, segments):
        """
        [关键逻辑] 将去程的 segments 翻转为回程 segments
        1. 翻转 domains 顺序
        2. 翻转每个 domain 内的 path
        3. 重新计算每个 domain 的 egress_iface
        """
        rev_segments = []
        # 翻转 Domain 列表
        reversed_list = segments[::-1]

        for i, seg in enumerate(reversed_list):
            c_name = seg['container']
            # 翻转路径节点列表
            rev_path = seg['path'][::-1]

            # 计算新的 Egress (回程的出口)
            new_egress = None
            if i < len(reversed_list) - 1:
                next_domain = reversed_list[i + 1]['container']
                # 查找 c_name -> next_domain 的连接
                new_egress = self._get_inter_link_exit(c_name, next_domain)

            rev_segments.append(
                {"container": c_name, "path": rev_path, "egress_iface": new_egress}
            )
        return rev_segments

    # ================= 检查与更新 =================

    def release_topology_allocation(self, segments, inter_path, released_bw):
        """释放旧路径占用带宽（用于同业务重部署）"""
        released_bw = float(released_bw)
        print(f"\n>>> Releasing Previous Allocation (+{released_bw} Mbps)...")

        updated_inter = False
        for i in range(len(inter_path) - 1):
            u, v = inter_path[i], inter_path[i + 1]
            idx = self._find_inter_link_idx(u, v)
            if idx != -1:
                qos = self.inter_links[idx].setdefault('qos', {})
                # 链路在故障态(bw=0)时，不恢复bw，但修正old_bw，保证恢复后容量正确。
                if qos.get('bw', 0) == 0 and 'old_bw' in qos:
                    old_val = qos.get('old_bw', 0)
                    qos['old_bw'] = old_val + released_bw
                    print(
                        f"    [Inter-Down] {u}<->{v}: old_bw {old_val} -> {qos['old_bw']} Mbps"
                    )
                    updated_inter = True
                elif qos.get('bw', 0) > 0:
                    old_bw = qos['bw']
                    qos['bw'] = old_bw + released_bw
                    print(f"    [Inter] {u}<->{v}: {old_bw} -> {qos['bw']} Mbps")
                    updated_inter = True
        if updated_inter:
            self._save_inter_links()

        for seg in segments:
            c_name = seg.get('container')
            if c_name not in self.container_topos:
                continue
            path = seg.get('path', [])
            topo_data = self.container_topos[c_name]
            updated_intra = False
            for i in range(len(path) - 1):
                u, v = path[i], path[i + 1]
                if u.startswith('s') and v.startswith('s'):
                    if u in topo_data['switches'] and v in topo_data['switches'][u]:
                        link_uv = topo_data['switches'][u][v]
                        link_vu = (
                            topo_data['switches'][v][u]
                            if (
                                v in topo_data['switches']
                                and u in topo_data['switches'][v]
                            )
                            else None
                        )

                        if link_uv.get('bw', 0) == 0 and 'old_bw' in link_uv:
                            old_val = link_uv.get('old_bw', 0)
                            new_val = old_val + released_bw
                            link_uv['old_bw'] = new_val
                            if isinstance(link_vu, dict):
                                link_vu['old_bw'] = new_val
                            print(
                                f"    [Intra-Down {c_name}] {u}<->{v}: old_bw {old_val} -> {new_val} Mbps"
                            )
                            updated_intra = True
                        elif link_uv.get('bw', 0) > 0:
                            old_bw = link_uv['bw']
                            new_bw = old_bw + released_bw
                            link_uv['bw'] = new_bw
                            if isinstance(link_vu, dict):
                                link_vu['bw'] = new_bw
                            print(
                                f"    [Intra {c_name}] {u}<->{v}: {old_bw} -> {new_bw} Mbps"
                            )
                            updated_intra = True
            if updated_intra:
                self._save_container_topo(c_name)

    def _clear_pair_flows(self, src_mac, dst_mac, target_segments):
        """
        仅清理目标路径涉及交换机上的旧规则，避免全网扫描导致耗时过高。
        """
        print("\n>>> Clearing stale flow rules for this host pair...")
        container_switches = self._collect_switches_from_segments(target_segments)
        for c_name, switch_set in container_switches.items():
            if not self._is_container_running(c_name):
                continue
            cmd_list = []
            for sw in sorted(switch_set):
                cmd_list.append(
                    f'ovs-ofctl --strict del-flows {sw} "priority=100,dl_src={src_mac},dl_dst={dst_mac}"'
                )
                cmd_list.append(
                    f'ovs-ofctl --strict del-flows {sw} "priority=100,dl_src={dst_mac},dl_dst={src_mac}"'
                )
            self._run_docker_batch(c_name, cmd_list)

    def _replace_existing_flow_if_needed(self, flow_key, src_mac, dst_mac):
        old_record = self.active_flows.get(flow_key)
        if old_record is None:
            return

        print(f"\n>>> Existing active flow found ({flow_key}), replacing it...")
        old_route = old_record.get('route', {})
        old_segments = old_route.get('segments', [])
        old_inter_path = old_route.get('inter_domain_path', [])
        old_bw = float(old_record.get('bw', 0))
        if old_bw > 0:
            self.release_topology_allocation(old_segments, old_inter_path, old_bw)

        self._clear_pair_flows(src_mac, dst_mac, old_segments)
        del self.active_flows[flow_key]
        self._save_active_flows()

    def _record_active_flow(
        self,
        flow_key,
        src,
        dst,
        bw_demand,
        route_data,
        path_idx,
        deployed_inter_path,
        fwd_segments,
    ):
        self.active_flows[flow_key] = {
            "flow_key": flow_key,
            "src": src,
            "dst": dst,
            "bw": float(bw_demand),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "route": {
                "path_index": path_idx,
                "score": route_data.get('score', 0),
                "inter_domain_path": copy.deepcopy(deployed_inter_path),
                "segments": copy.deepcopy(fwd_segments),
            },
        }
        self._save_active_flows()
        print(f"    Active flow state updated: {ACTIVE_FLOW_FILE}")

    def check_bandwidth_availability(self, segments, inter_path, required_bw):
        """部署前检查带宽"""
        print(f"  Checking bandwidth availability (Need {required_bw} Mbps)...")
        # 1. 域间
        for i in range(len(inter_path) - 1):
            u, v = inter_path[i], inter_path[i + 1]
            idx = self._find_inter_link_idx(u, v)
            if idx == -1:
                return False
            if self.inter_links[idx]['qos']['bw'] < required_bw:
                print(f"    [Fail] Inter-link {u}-{v} BW insufficient.")
                return False
        # 2. 域内
        for seg in segments:
            c_name = seg['container']
            path = seg['path']
            topo = self.container_topos.get(c_name)
            for i in range(len(path) - 1):
                u, v = path[i], path[i + 1]
                if u.startswith('s') and v.startswith('s'):
                    if u in topo['switches'] and v in topo['switches'][u]:
                        if topo['switches'][u][v]['bw'] < required_bw:
                            print(f"    [Fail] {c_name} {u}-{v} BW insufficient.")
                            return False
        print("    [Pass] Bandwidth check passed.")
        return True

    def update_topology_files(self, segments, inter_path, required_bw):
        """更新 JSON 文件 (只扣一次)"""
        print(f"\n>>> Updating Network State (Subtracting {required_bw} Mbps)...")
        updated_inter = False
        for i in range(len(inter_path) - 1):
            u, v = inter_path[i], inter_path[i + 1]
            idx = self._find_inter_link_idx(u, v)
            if idx != -1:
                old_bw = self.inter_links[idx]['qos']['bw']
                self.inter_links[idx]['qos']['bw'] = max(0, old_bw - required_bw)
                print(
                    f"    [Inter] {u}<->{v}: {old_bw} -> {self.inter_links[idx]['qos']['bw']} Mbps"
                )
                updated_inter = True

        if updated_inter:
            with open(os.path.join(TOPO_DIR, INTER_DOMAIN_FILE), 'w') as f:
                json.dump(self.inter_links, f, indent=4)

        for seg in segments:
            c_name = seg['container']
            path = seg['path']
            topo_data = self.container_topos[c_name]
            updated_intra = False
            for i in range(len(path) - 1):
                u, v = path[i], path[i + 1]
                if u.startswith('s') and v.startswith('s'):
                    if u in topo_data['switches'] and v in topo_data['switches'][u]:
                        old_bw = topo_data['switches'][u][v]['bw']
                        new_bw = max(0, old_bw - required_bw)
                        topo_data['switches'][u][v]['bw'] = new_bw
                        if v in topo_data['switches'] and u in topo_data['switches'][v]:
                            topo_data['switches'][v][u]['bw'] = new_bw
                        print(
                            f"    [Intra {c_name}] {u}<->{v}: {old_bw} -> {new_bw} Mbps"
                        )
                        updated_intra = True
            if updated_intra:
                with open(os.path.join(TOPO_DIR, f"{c_name}_topo.json"), 'w') as f:
                    json.dump(topo_data, f, indent=4)

    # ================= 部署逻辑 =================

    def _install_flows(self, segment, match_src, match_dst):
        c_name = segment['container']
        path = segment['path']
        egress_iface = segment['egress_iface']
        topo = self.container_topos[c_name]

        path_str = " -> ".join(path)
        exit_info = f"(Exit: {egress_iface})" if egress_iface else "(Endpoint)"
        print(f"  [Domain: {c_name}] Path: {path_str} {exit_info}")

        cmd_list = []
        for i in range(len(path)):
            curr = path[i]
            if curr.startswith('h'):
                continue

            out_port = None
            if i < len(path) - 1:
                next_node = path[i + 1]
                # 查表找端口
                if curr in topo['switches'] and next_node in topo['switches'][curr]:
                    info = topo['switches'][curr][next_node]
                    out_port = info['port'] if isinstance(info, dict) else info
            elif egress_iface:
                out_port = egress_iface

            if out_port:
                print(
                    f"    [Flow] {c_name} {curr}: Match SRC={match_src} DST={match_dst} -> Output {out_port}"
                )
                cmd_list.append(
                    f'ovs-ofctl add-flow {curr} "priority=100,dl_src={match_src},dl_dst={match_dst},actions=output:{out_port}"'
                )
        self._run_docker_batch(c_name, cmd_list)

    def deploy_path(self, src, dst, bw_demand, path_idx=0):
        # 1. 读取保存的路径 (按排序后的文件名查找)
        safe_src = src.replace(':', '_')
        safe_dst = dst.replace(':', '_')
        p1, p2 = sorted([safe_src, safe_dst])
        filename = f"{p1}_{p2}_{int(bw_demand)}.json"

        load_path = os.path.join(PATH_SAVE_DIR, filename)
        if not os.path.exists(load_path):
            print(f"[Error] Path file not found: {load_path}")
            print(
                f"Please run 'sudo python3 route_cal.py {src} {dst} {bw_demand}' first."
            )
            sys.exit(1)

        with open(load_path, 'r') as f:
            candidates = json.load(f)

        if path_idx >= len(candidates):
            print(f"[Error] Index {path_idx} out of range.")
            sys.exit(1)

        route_data = candidates[path_idx]
        print(
            f"\n>>> Deploying Route (Rank {path_idx}, Score={route_data['score']:.2f})"
        )
        print(f"    Inter-Domain: {'->'.join(route_data['inter_domain_path'])}")

        # 2. 准备 IP/MAC 信息
        src_c, src_h = src.split(':')
        dst_c, dst_h = dst.split(':')
        src_info = self.container_topos[src_c]['hosts'][src_h]
        dst_info = self.container_topos[dst_c]['hosts'][dst_h]

        # 3. 若是同一业务重部署：先释放旧占用并清旧规则
        flow_key = self._flow_key(src, dst, bw_demand)
        self._replace_existing_flow_if_needed(flow_key, src_info['mac'], dst_info['mac'])

        # 4. 检查带宽
        if not self.check_bandwidth_availability(
            route_data['segments'], route_data['inter_domain_path'], bw_demand
        ):
            print("\n[Error] Bandwidth check failed.")
            return

        # 5. 识别方向
        # 文件里保存的是 saved_src -> saved_dst
        saved_src_str = route_data['src_dst'][0]
        # 如果用户输入的 src 和文件保存的 src 一致，则 forward=segments
        # 如果不一致 (反向)，则 forward=invert(segments)
        is_direct = src == saved_src_str

        raw_segments = route_data['segments']

        if is_direct:
            fwd_segments = raw_segments
            rev_segments = self.invert_segments(raw_segments)
        else:
            fwd_segments = self.invert_segments(raw_segments)
            rev_segments = raw_segments

        # 6. 配置主机 (双向)
        print(f"\n=== 1. Configuring Hosts ({src} <---> {dst}) ===")
        self._run_docker_batch(
            src_c,
            [
                f"m {src_h} ip route replace {dst_info['ip']}/32 dev {src_h}-eth0",
                f"m {src_h} arp -s {dst_info['ip']} {dst_info['mac']}",
            ],
        )
        self._run_docker_batch(
            dst_c,
            [
                f"m {dst_h} ip route replace {src_info['ip']}/32 dev {dst_h}-eth0",
                f"m {dst_h} arp -s {src_info['ip']} {src_info['mac']}",
            ],
        )

        # 7. 下发流表 (自动双向)
        print("\n=== 2. Installing Forward Flows (Src -> Dst) ===")
        for seg in fwd_segments:
            self._install_flows(seg, src_info['mac'], dst_info['mac'])

        print("\n=== 3. Installing Reverse Flows (Dst -> Src) ===")
        for seg in rev_segments:
            self._install_flows(seg, dst_info['mac'], src_info['mac'])

        # 8. 更新状态
        self.update_topology_files(
            route_data['segments'], route_data['inter_domain_path'], bw_demand
        )

        deployed_inter_path = (
            route_data['inter_domain_path']
            if is_direct
            else list(reversed(route_data['inter_domain_path']))
        )
        self._record_active_flow(
            flow_key,
            src,
            dst,
            bw_demand,
            route_data,
            path_idx,
            deployed_inter_path,
            fwd_segments,
        )

        print("\n>>> Deployment & Update Complete.")


if __name__ == "__main__":
    # 记录开始时间
    start_time = time.time()

    parser = argparse.ArgumentParser()
    parser.add_argument("src", help="Source node (e.g. docker1:h1)")
    parser.add_argument("dst", help="Destination node")
    parser.add_argument("bw", type=float, help="Bandwidth demand")
    parser.add_argument(
        "--index", type=int, default=0, help="Path rank index to deploy (default 0)"
    )

    args = parser.parse_args()

    deployer = RouteDeployer()
    deployer.deploy_path(args.src, args.dst, args.bw, args.index)

    # 计算并打印结束时间
    end_time = time.time()
    print(f"\n[Time] Total Execution Time: {end_time - start_time:.4f} seconds")
