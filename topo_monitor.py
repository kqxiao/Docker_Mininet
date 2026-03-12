#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
import subprocess
import time

# ================= 配置 =================
TOPO_DIR = "topo_data"
PATH_DIR = "topo_path"
INTER_DOMAIN_FILE = os.path.join(TOPO_DIR, "inter_domain_db.json")
ACTIVE_FLOW_FILE = os.path.join(TOPO_DIR, "active_flows.json")
# =======================================


def _with_sudo(cmd):
    """root 下直接执行，非 root 自动补 sudo。"""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return cmd
    return f"sudo {cmd}"


def _normalize_bw_str(bw):
    bw_f = float(bw)
    if bw_f.is_integer():
        return str(int(bw_f))
    return f"{bw_f:.3f}".rstrip("0").rstrip(".")


def _extract_exec_time(stdout_text):
    m = re.search(r"Total Execution Time:\s*([0-9.]+)\s*seconds", stdout_text or "")
    if m:
        return float(m.group(1))
    return None


def _build_path_filename(src, dst, bw):
    safe_src = src.replace(":", "_")
    safe_dst = dst.replace(":", "_")
    p1, p2 = sorted([safe_src, safe_dst])
    return f"{p1}_{p2}_{int(float(bw))}.json"


def _load_best_path_display(src, dst, bw):
    """
    从 topo_path 读取 rank=0 的路径，并转成展示字符串。
    """
    path_file = os.path.join(PATH_DIR, _build_path_filename(src, dst, bw))
    if not os.path.exists(path_file):
        return None
    try:
        with open(path_file, "r") as f:
            data = json.load(f)
        if not isinstance(data, list) or not data:
            return None
        cand = data[0]
        full = []
        for seg in cand.get("segments", []):
            c_name = seg.get("container")
            for node in seg.get("path", []):
                if c_name and node:
                    full.append(f"{c_name}-{node}")
        cleaned = []
        for n in full:
            if not cleaned or cleaned[-1] != n:
                cleaned.append(n)
        if len(cleaned) < 2:
            return None
        return " -> ".join(n.replace("docker", "domain") for n in cleaned)
    except Exception:
        return None


def run_cmd(cmd):
    try:
        output = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT)
        return True, output.decode()
    except subprocess.CalledProcessError as e:
        return False, e.output.decode()


def is_container_running(container_name):
    ok, output = run_cmd(
        _with_sudo(f"docker inspect -f '{{{{.State.Running}}}}' {container_name}")
    )
    return ok and output.strip().lower() == "true"


def is_switch_alive(container_name, switch_name, container_states):
    if not container_states.get(container_name, False):
        return False
    ok, _ = run_cmd(_with_sudo(f"docker exec {container_name} ovs-vsctl br-exists {switch_name}"))
    if not ok:
        return False
    ok, _ = run_cmd(_with_sudo(f"docker exec {container_name} ovs-ofctl show {switch_name}"))
    return ok


def get_container_iface_status(container_name, iface, container_states=None):
    """
    返回 True (UP) 或 False (DOWN/Error)
    """
    if container_states is not None and not container_states.get(container_name, False):
        return False

    ok, output = run_cmd(_with_sudo(f"docker exec {container_name} ip -o link show {iface}"))
    if not ok:
        return False

    if "state DOWN" in output:
        return False
    return "UP" in output or "LOWER_UP" in output


def load_active_flows():
    if not os.path.exists(ACTIVE_FLOW_FILE):
        return {}
    try:
        with open(ACTIVE_FLOW_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {
                item["flow_key"]: item
                for item in data
                if isinstance(item, dict) and "flow_key" in item
            }
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"[Warning] Failed to read active flow db: {e}")
    return {}


def save_active_flows(flow_dict):
    data = [flow_dict[k] for k in sorted(flow_dict.keys())]
    with open(ACTIVE_FLOW_FILE, "w") as f:
        json.dump(data, f, indent=4)


def _find_inter_link_idx(links, u_c, v_c):
    for i, link in enumerate(links):
        if (link['src_container'] == u_c and link['dst_container'] == v_c) or (
            link['src_container'] == v_c and link['dst_container'] == u_c
        ):
            return i
    return -1


def _load_intra_topologies():
    topos = {}
    if not os.path.exists(TOPO_DIR):
        return topos
    for filename in os.listdir(TOPO_DIR):
        if filename.endswith("_topo.json"):
            c_name = filename.split("_")[0]
            path = os.path.join(TOPO_DIR, filename)
            try:
                with open(path, "r") as f:
                    topos[c_name] = json.load(f)
            except Exception as e:
                print(f"[Warning] Failed to load {path}: {e}")
    return topos


def _iter_unique_switch_links(topo):
    seen = set()
    switches = topo.get("switches", {})
    for u, nbrs in switches.items():
        if not u.startswith("s"):
            continue
        for v, info_uv in nbrs.items():
            if not v.startswith("s"):
                continue
            if v not in switches or u not in switches[v]:
                continue
            info_vu = switches[v][u]
            if not isinstance(info_uv, dict) or not isinstance(info_vu, dict):
                continue
            key = tuple(sorted((u, v)))
            if key in seen:
                continue
            seen.add(key)
            yield u, v, info_uv, info_vu


def _set_intra_link_down(topo, u, v):
    info_uv = topo["switches"][u][v]
    info_vu = topo["switches"][v][u]
    current_bw = info_uv.get("bw", 0)
    if current_bw > 0:
        info_uv["old_bw"] = current_bw
        info_vu["old_bw"] = current_bw
        info_uv["bw"] = 0
        info_vu["bw"] = 0
        return True, current_bw
    return False, current_bw


def _set_intra_link_up(topo, u, v):
    info_uv = topo["switches"][u][v]
    info_vu = topo["switches"][v][u]
    if info_uv.get("bw", 0) == 0 and "old_bw" in info_uv:
        restored = info_uv["old_bw"]
        info_uv["bw"] = restored
        info_vu["bw"] = restored
        return True, restored
    return False, 0


def release_flow_reservation(flow_record):
    """
    故障重路由前释放旧路径资源，避免重复扣带宽。
    """
    route = flow_record.get("route", {})
    segments = route.get("segments", [])
    inter_path = route.get("inter_domain_path", [])
    bw = float(flow_record.get("bw", 0))
    if bw <= 0:
        return

    print(
        f"  Releasing old reservation: {flow_record.get('src')} -> {flow_record.get('dst')} (+{bw} Mbps)"
    )

    if os.path.exists(INTER_DOMAIN_FILE):
        with open(INTER_DOMAIN_FILE, "r") as f:
            inter_links = json.load(f)

        updated_inter = False
        for i in range(len(inter_path) - 1):
            u, v = inter_path[i], inter_path[i + 1]
            idx = _find_inter_link_idx(inter_links, u, v)
            if idx == -1:
                continue
            qos = inter_links[idx].setdefault("qos", {})

            if qos.get("bw", 0) == 0 and "old_bw" in qos:
                old_val = qos.get("old_bw", 0)
                qos["old_bw"] = old_val + bw
                print(
                    f"    [Inter-Down] {u}<->{v}: old_bw {old_val} -> {qos['old_bw']} Mbps"
                )
                updated_inter = True
            elif qos.get("bw", 0) > 0:
                old_val = qos["bw"]
                qos["bw"] = old_val + bw
                print(f"    [Inter] {u}<->{v}: {old_val} -> {qos['bw']} Mbps")
                updated_inter = True

        if updated_inter:
            with open(INTER_DOMAIN_FILE, "w") as f:
                json.dump(inter_links, f, indent=4)

    # 域内释放
    for seg in segments:
        c_name = seg.get("container")
        path = seg.get("path", [])
        topo_file = os.path.join(TOPO_DIR, f"{c_name}_topo.json")
        if not c_name or not os.path.exists(topo_file):
            continue

        with open(topo_file, "r") as f:
            topo = json.load(f)

        updated = False
        for i in range(len(path) - 1):
            u, v = path[i], path[i + 1]
            if not (u.startswith("s") and v.startswith("s")):
                continue
            if u in topo["switches"] and v in topo["switches"][u]:
                link_uv = topo["switches"][u][v]
                link_vu = (
                    topo["switches"][v][u]
                    if (v in topo["switches"] and u in topo["switches"][v])
                    else None
                )
                if link_uv.get("bw", 0) == 0 and "old_bw" in link_uv:
                    old_val = link_uv.get("old_bw", 0)
                    new_val = old_val + bw
                    link_uv["old_bw"] = new_val
                    if isinstance(link_vu, dict):
                        link_vu["old_bw"] = new_val
                    print(
                        f"    [Intra-Down {c_name}] {u}<->{v}: old_bw {old_val} -> {new_val} Mbps"
                    )
                    updated = True
                elif link_uv.get("bw", 0) > 0:
                    old_bw = link_uv["bw"]
                    new_bw = old_bw + bw
                    link_uv["bw"] = new_bw
                    if isinstance(link_vu, dict):
                        link_vu["bw"] = new_bw
                    print(f"    [Intra {c_name}] {u}<->{v}: {old_bw} -> {new_bw} Mbps")
                    updated = True

        if updated:
            with open(topo_file, "w") as f:
                json.dump(topo, f, indent=4)


def _flow_impacted_by_failures(
    flow_record,
    inter_failure_links,
    node_failures,
    intra_link_failures,
    intra_switch_failures,
):
    route = flow_record.get("route", {})
    inter_path = route.get("inter_domain_path", [])
    segments = route.get("segments", [])

    failed_nodes = set(node_failures)
    if failed_nodes:
        for node in inter_path:
            if node in failed_nodes:
                return True
        for seg in segments:
            if seg.get("container") in failed_nodes:
                return True

    failed_pairs = {
        frozenset((item["src_container"], item["dst_container"]))
        for item in inter_failure_links
    }
    for i in range(len(inter_path) - 1):
        if frozenset((inter_path[i], inter_path[i + 1])) in failed_pairs:
            return True

    failed_switches = {
        (item["container"], item["switch"]) for item in intra_switch_failures
    }
    if failed_switches:
        for seg in segments:
            c_name = seg.get("container")
            for node in seg.get("path", []):
                if node.startswith("s") and (c_name, node) in failed_switches:
                    return True

    failed_intra_links = {
        (item["container"], frozenset((item["u"], item["v"])))
        for item in intra_link_failures
    }
    if failed_intra_links:
        for seg in segments:
            c_name = seg.get("container")
            path = seg.get("path", [])
            for i in range(len(path) - 1):
                u, v = path[i], path[i + 1]
                if not (u.startswith("s") and v.startswith("s")):
                    continue
                if (c_name, frozenset((u, v))) in failed_intra_links:
                    return True

    return False


def reroute_flow(flow_record):
    src = flow_record.get("src")
    dst = flow_record.get("dst")
    bw = flow_record.get("bw")
    if not src or not dst or bw is None:
        return {
            "success": False,
            "route_gen_time": None,
            "route_switch_time": None,
            "path": None,
        }

    bw_str = _normalize_bw_str(bw)
    print(f"\n>>> Re-routing {src} -> {dst} (BW={bw_str} Mbps)")

    cmd_cal = _with_sudo(f"python3 route_cal.py {src} {dst} {bw_str}")
    t0 = time.time()
    ok, out = run_cmd(cmd_cal)
    route_gen_time = _extract_exec_time(out)
    if route_gen_time is None:
        route_gen_time = time.time() - t0
    if not ok:
        print(f"  [Fail] route_cal failed for {src}->{dst}")
        print(out.strip())
        return {
            "success": False,
            "route_gen_time": route_gen_time,
            "route_switch_time": None,
            "path": None,
        }

    path_display = _load_best_path_display(src, dst, bw_str)

    cmd_deploy = _with_sudo(f"python3 route_path.py {src} {dst} {bw_str} --index 0")
    t1 = time.time()
    ok, out = run_cmd(cmd_deploy)
    route_switch_time = _extract_exec_time(out)
    if route_switch_time is None:
        route_switch_time = time.time() - t1
    if not ok:
        print(f"  [Fail] route_path failed for {src}->{dst}")
        print(out.strip())
        return {
            "success": False,
            "route_gen_time": route_gen_time,
            "route_switch_time": route_switch_time,
            "path": path_display,
        }

    print(f"  [OK] Re-route deployed for {src}->{dst}")
    if path_display:
        print(f"  [REROUTE] Path: {path_display}")
    else:
        print("  [REROUTE] Path: <unavailable>")
    print(
        f"  [REROUTE] Time: route_gen={route_gen_time:.4f}s | route_switch={route_switch_time:.4f}s"
    )
    return {
        "success": True,
        "route_gen_time": route_gen_time,
        "route_switch_time": route_switch_time,
        "path": path_display,
    }


def handle_failures_and_reroute(
    inter_failure_links,
    node_failures,
    intra_link_failures,
    intra_switch_failures,
):
    active_flows = load_active_flows()
    if not active_flows:
        return

    impacted = []
    for flow_key, flow_record in active_flows.items():
        if _flow_impacted_by_failures(
            flow_record,
            inter_failure_links,
            node_failures,
            intra_link_failures,
            intra_switch_failures,
        ):
            impacted.append((flow_key, flow_record))

    if not impacted:
        print("  No active flow is affected.")
        return

    print(f"  Impacted active flows: {len(impacted)}")
    for flow_key, flow_record in impacted:
        print(
            f"    - {flow_record.get('src')} -> {flow_record.get('dst')} ({flow_record.get('bw')} Mbps)"
        )
        release_flow_reservation(flow_record)
        active_flows.pop(flow_key, None)

    save_active_flows(active_flows)

    reroute_results = []
    for _, flow_record in impacted:
        reroute_results.append(reroute_flow(flow_record))

    success_results = [r for r in reroute_results if r.get("success")]
    if success_results:
        avg_route = sum(r["route_gen_time"] for r in success_results) / len(success_results)
        avg_switch = sum(r["route_switch_time"] for r in success_results) / len(success_results)
        print(
            f"[REROUTE AVG] route_gen={avg_route:.4f}s route_switch={avg_switch:.4f}s flows={len(success_results)}"
        )


def sync_inter_domain_topology(last_container_states):
    if not os.path.exists(INTER_DOMAIN_FILE):
        print(f"[Warning] {INTER_DOMAIN_FILE} not found. Waiting for deployment...")
        return {
            "failure_links": [],
            "recovered_links": [],
            "node_failures": [],
            "node_recoveries": [],
            "container_states": last_container_states,
        }

    print(f"\n[{time.strftime('%H:%M:%S')}] Monitoring Inter-Domain Topology...")

    try:
        with open(INTER_DOMAIN_FILE, "r") as f:
            links = json.load(f)
    except Exception as e:
        print(f"[Error] Failed to read JSON: {e}")
        return {
            "failure_links": [],
            "recovered_links": [],
            "node_failures": [],
            "node_recoveries": [],
            "container_states": last_container_states,
        }

    all_containers = set()
    for link in links:
        all_containers.add(link['src_container'])
        all_containers.add(link['dst_container'])

    # 一并扫描有 topo 文件但当前没有出现在 inter_domain_db.json 的容器
    for c_name in _load_intra_topologies().keys():
        all_containers.add(c_name)

    container_states = {c: is_container_running(c) for c in all_containers}
    node_failures = []
    node_recoveries = []
    for c_name, is_up in container_states.items():
        was_up = last_container_states.get(c_name, True)
        if was_up and not is_up:
            node_failures.append(c_name)
            print(f"  [!] Node Failure: {c_name} is DOWN")
        elif not was_up and is_up:
            node_recoveries.append(c_name)
            print(f"  [*] Node Recovery: {c_name} is UP")

    changed = False
    failure_links = []
    recovered_links = []
    for link in links:
        s_c = link['src_container']
        s_if = link['src_iface']
        d_c = link['dst_container']
        d_if = link['dst_iface']

        src_up = get_container_iface_status(s_c, s_if, container_states)
        dst_up = get_container_iface_status(d_c, d_if, container_states)

        is_alive = src_up and dst_up
        current_bw = link['qos']['bw']

        if not is_alive and current_bw > 0:
            print(f"  [!] Link Failure: {s_c}:{s_if} <-> {d_c}:{d_if}")
            if not src_up:
                print(f"      -> {s_c}:{s_if} DOWN")
            if not dst_up:
                print(f"      -> {d_c}:{d_if} DOWN")

            link['qos']['old_bw'] = current_bw
            link['qos']['bw'] = 0
            changed = True
            failure_links.append(
                {
                    "src_container": s_c,
                    "src_iface": s_if,
                    "dst_container": d_c,
                    "dst_iface": d_if,
                }
            )
        elif is_alive and current_bw == 0 and 'old_bw' in link['qos']:
            restored = link['qos']['old_bw']
            link['qos']['bw'] = restored
            changed = True
            recovered_links.append(
                {
                    "src_container": s_c,
                    "src_iface": s_if,
                    "dst_container": d_c,
                    "dst_iface": d_if,
                }
            )
            print(f"  [*] Link Recovery: {s_c}:{s_if} <-> {d_c}:{d_if} (BW={restored})")

    if changed:
        with open(INTER_DOMAIN_FILE, "w") as f:
            json.dump(links, f, indent=4)
        print("  >>> Inter-domain topology db updated.")

    return {
        "failure_links": failure_links,
        "recovered_links": recovered_links,
        "node_failures": node_failures,
        "node_recoveries": node_recoveries,
        "container_states": container_states,
    }


def sync_intra_domain_topology(container_states, last_switch_states):
    print(f"[{time.strftime('%H:%M:%S')}] Monitoring Intra-Domain Topology...")
    topos = _load_intra_topologies()

    intra_link_failures = []
    intra_link_recoveries = []
    intra_switch_failures = []
    intra_switch_recoveries = []
    switch_states = {}

    for c_name, topo in topos.items():
        switches = topo.get("switches", {})
        local_switch_status = {}
        for sw in switches.keys():
            if not sw.startswith("s"):
                continue
            key = f"{c_name}:{sw}"
            alive = is_switch_alive(c_name, sw, container_states)
            local_switch_status[sw] = alive
            switch_states[key] = alive

            was_alive = last_switch_states.get(key, True)
            if was_alive and not alive:
                intra_switch_failures.append({"container": c_name, "switch": sw})
                print(f"  [!] Intra Switch Failure: {c_name}:{sw}")
            elif not was_alive and alive:
                intra_switch_recoveries.append({"container": c_name, "switch": sw})
                print(f"  [*] Intra Switch Recovery: {c_name}:{sw}")

        changed = False
        link_failure_keys = set()
        link_recovery_keys = set()

        # 1) 交换机 down 时，把其相关链路置 0
        for sw, alive in local_switch_status.items():
            if alive:
                continue
            if sw not in switches:
                continue
            for nbr in list(switches[sw].keys()):
                if not nbr.startswith("s"):
                    continue
                if nbr not in switches or sw not in switches[nbr]:
                    continue
                key = tuple(sorted((sw, nbr)))
                changed_now, old_bw = _set_intra_link_down(topo, sw, nbr)
                if changed_now:
                    changed = True
                    if key not in link_failure_keys:
                        link_failure_keys.add(key)
                        intra_link_failures.append(
                            {
                                "container": c_name,
                                "u": key[0],
                                "v": key[1],
                                "reason": "switch_down",
                            }
                        )
                        print(
                            f"  [!] Intra Link Failure: {c_name}:{key[0]}<->{key[1]} (switch down, bw {old_bw} -> 0)"
                        )

        # 2) 交换机存活时，检测接口是否 down/up
        for u, v, info_uv, info_vu in _iter_unique_switch_links(topo):
            if not (local_switch_status.get(u, False) and local_switch_status.get(v, False)):
                continue

            port_u = info_uv.get("port")
            port_v = info_vu.get("port")
            up_u = get_container_iface_status(c_name, port_u, container_states)
            up_v = get_container_iface_status(c_name, port_v, container_states)
            is_alive = up_u and up_v
            current_bw = info_uv.get("bw", 0)
            key = tuple(sorted((u, v)))

            if not is_alive and current_bw > 0:
                changed_now, old_bw = _set_intra_link_down(topo, u, v)
                if changed_now:
                    changed = True
                    if key not in link_failure_keys:
                        link_failure_keys.add(key)
                        intra_link_failures.append(
                            {
                                "container": c_name,
                                "u": key[0],
                                "v": key[1],
                                "u_port": port_u,
                                "v_port": port_v,
                                "reason": "port_down",
                            }
                        )
                        print(
                            f"  [!] Intra Link Failure: {c_name}:{u}:{port_u} <-> {v}:{port_v} (bw {old_bw} -> 0)"
                        )
            elif is_alive and current_bw == 0 and "old_bw" in info_uv:
                changed_now, restored = _set_intra_link_up(topo, u, v)
                if changed_now:
                    changed = True
                    if key not in link_recovery_keys:
                        link_recovery_keys.add(key)
                        intra_link_recoveries.append(
                            {
                                "container": c_name,
                                "u": key[0],
                                "v": key[1],
                                "u_port": port_u,
                                "v_port": port_v,
                            }
                        )
                        print(
                            f"  [*] Intra Link Recovery: {c_name}:{u}:{port_u} <-> {v}:{port_v} (bw {restored})"
                        )

        if changed:
            topo_file = os.path.join(TOPO_DIR, f"{c_name}_topo.json")
            with open(topo_file, "w") as f:
                json.dump(topo, f, indent=4)
            print(f"  >>> Intra topology db updated: {topo_file}")

    return {
        "intra_link_failures": intra_link_failures,
        "intra_link_recoveries": intra_link_recoveries,
        "intra_switch_failures": intra_switch_failures,
        "intra_switch_recoveries": intra_switch_recoveries,
        "switch_states": switch_states,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--interval", type=float, default=5.0, help="Scan interval seconds"
    )
    args = parser.parse_args()

    print("======================================================")
    print("  Topology Monitor + Auto ReRoute (Inter + Intra)")
    print("======================================================")

    if not os.path.exists(TOPO_DIR):
        os.makedirs(TOPO_DIR)

    last_container_states = {}
    last_switch_states = {}
    try:
        while True:
            cycle_start = time.time()
            inter_events = sync_inter_domain_topology(last_container_states)
            last_container_states = inter_events["container_states"]

            intra_events = sync_intra_domain_topology(
                inter_events["container_states"], last_switch_states
            )
            last_switch_states = intra_events["switch_states"]

            if (
                inter_events["failure_links"]
                or inter_events["node_failures"]
                or intra_events["intra_link_failures"]
                or intra_events["intra_switch_failures"]
            ):
                handle_failures_and_reroute(
                    inter_events["failure_links"],
                    inter_events["node_failures"],
                    intra_events["intra_link_failures"],
                    intra_events["intra_switch_failures"],
                )

            elapsed = time.time() - cycle_start
            sleep_s = max(0.0, args.interval - elapsed)
            time.sleep(sleep_s)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
