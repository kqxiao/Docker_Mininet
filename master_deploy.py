#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import time
import os
import argparse

# 配置
DOMAIN_SCRIPT_OUT = "autobuild_out.py"
DOMAIN_SCRIPT_IN = "autobuild_in_agent.py"
NUM_CONTAINERS = 5
TOPO_READY_TIMEOUT = 180  # 等待拓扑就绪最长时间（秒）
TOPO_POLL_INTERVAL = 2    # 就绪检测轮询间隔（秒）


def run_cmd(cmd):
    print(f"Exec: {cmd}")
    try:
        subprocess.check_call(cmd, shell=True)
    except subprocess.CalledProcessError:
        print(f"Error executing {cmd}")
        sys.exit(1)


def run_cmd_ok(cmd):
    """
    执行命令并返回是否成功（不抛异常，不退出）。
    """
    result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.returncode == 0


def container_topo_ready(container_name):
    """
    检测容器内 /root/topo_db.json 是否已生成且为合法 JSON。
    """
    check_cmd = (
        f"sudo docker exec {container_name} "
        "python3 -c \"import json; json.load(open('/root/topo_db.json'))\""
    )
    return run_cmd_ok(check_cmd)


def reset_runtime_state():
    """
    清理历史运行状态，避免旧业务流/旧路径影响新一轮部署。
    """
    topo_data_dir = "topo_data"
    topo_path_dir = "topo_path"

    # 1) 清理 topo_path 下的历史候选路径
    if not os.path.exists(topo_path_dir):
        os.makedirs(topo_path_dir)
    else:
        for name in os.listdir(topo_path_dir):
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(topo_path_dir, name))
                except Exception:
                    pass

    # 2) 清理 topo_data 下的历史活动流和旧拓扑快照
    if not os.path.exists(topo_data_dir):
        os.makedirs(topo_data_dir)
    else:
        for name in os.listdir(topo_data_dir):
            if name == "active_flows.json" or name.endswith("_topo.json") or name == "inter_domain_db.json":
                try:
                    os.remove(os.path.join(topo_data_dir, name))
                except Exception:
                    pass


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build inter/intra domain network with selectable topology presets."
    )
    parser.add_argument(
        "--inter-preset",
        default="ba_core",
        help="Inter-domain topology preset passed to autobuild_out.py",
    )
    parser.add_argument(
        "--inter-seed",
        type=int,
        default=1,
        help="Seed for inter-domain topology/QoS generation",
    )
    parser.add_argument(
        "--intra-preset",
        default="ba_core",
        help="Intra-domain topology preset passed to autobuild_in_agent.py",
    )
    parser.add_argument(
        "--intra-seed-base",
        type=int,
        default=1,
        help="Base seed for intra-domain generation; docker i uses seed=(base+i-1)",
    )
    parser.add_argument(
        "--list-inter-presets",
        action="store_true",
        help="List inter-domain presets and exit",
    )
    parser.add_argument(
        "--list-intra-presets",
        action="store_true",
        help="List intra-domain presets and exit",
    )
    return parser.parse_args()


def main(args):
    if args.list_inter_presets:
        run_cmd(f"python3 {DOMAIN_SCRIPT_OUT} --list-presets")
        return
    if args.list_intra_presets:
        run_cmd(f"python3 {DOMAIN_SCRIPT_IN} --list-presets")
        return

    # 0. 清理历史状态
    print("\n" + "=" * 40)
    print("STEP 0: 清理历史状态 (topo_data/topo_path)")
    print("=" * 40)
    reset_runtime_state()

    # 1. 运行域间网络构建
    print("\n" + "=" * 40)
    print("STEP 1: 构建域间网络 (Docker + OVS)")
    print("=" * 40)
    run_cmd(
        f"sudo python3 {DOMAIN_SCRIPT_OUT} --preset {args.inter_preset} --seed {args.inter_seed}"
    )

    # 2. 部署域内网络
    print("\n" + "=" * 40)
    print("STEP 2: 部署域内网络 (Mininet inside Docker)")
    print("=" * 40)
    print(f"Inter preset: {args.inter_preset} (seed={args.inter_seed})")
    print(f"Intra preset: {args.intra_preset} (seed base={args.intra_seed_base})")

    # 准备数据目录
    if not os.path.exists("topo_data"):
        os.makedirs("topo_data")

    # 移动域间数据
    if os.path.exists("inter_domain_db.json"):
        run_cmd("mv inter_domain_db.json topo_data/")

    for i in range(1, NUM_CONTAINERS + 1):
        container_name = f"docker{i}"
        seed = args.intra_seed_base + i - 1

        print(f"\n[Deploying {container_name}] Seed={seed}, DockerID={i}...")

        # 拷贝脚本
        run_cmd(
            f"sudo docker cp {DOMAIN_SCRIPT_IN} {container_name}:/root/topo_agent.py"
        )

        # 后台运行 (传入 --id)
        cmd = (
            f"sudo docker exec -d {container_name} python3 /root/topo_agent.py "
            f"--seed {seed} --id {i} --preset {args.intra_preset}"
        )
        run_cmd(cmd)

        print(f"  -> {container_name} started.")

    # 3. 轮询就绪并回收拓扑数据（替代固定等待）
    print("\n" + "=" * 40)
    print("STEP 3: 收集拓扑数据")
    print("=" * 40)
    print(
        f"开始主动检测拓扑就绪（超时 {TOPO_READY_TIMEOUT}s，轮询间隔 {TOPO_POLL_INTERVAL}s）..."
    )

    pending = {f"docker{i}" for i in range(1, NUM_CONTAINERS + 1)}
    deadline = time.time() + TOPO_READY_TIMEOUT

    while pending and time.time() < deadline:
        for c_name in list(pending):
            if container_topo_ready(c_name):
                print(f"  -> {c_name} topo_db.json 已就绪，开始拷贝...")
                if run_cmd_ok(
                    f"sudo docker cp {c_name}:/root/topo_db.json ./topo_data/{c_name}_topo.json"
                ):
                    pending.remove(c_name)
                    print(f"     [OK] {c_name} 拓扑数据已回收")
                else:
                    print(f"     [Warning] {c_name} 拷贝失败，稍后重试")

        if pending:
            print(f"  ...等待中，未就绪: {', '.join(sorted(pending))}")
            time.sleep(TOPO_POLL_INTERVAL)

    # 超时后的兜底提示
    if pending:
        print("\n[Warning] 以下容器在超时时间内未完成拓扑导出：")
        for c_name in sorted(pending):
            print(f"  - {c_name}")
        print("可稍后手动执行 docker cp 获取对应 topo_db.json。")

    print("\n" + "=" * 40)
    print("DEPLOYMENT COMPLETE")
    print("=" * 40)
    print("所有网络已启动，拓扑数据已保存在 ./topo_data/ 目录下。")
    print("通过 sudo python3 route_cal.py docker2:h1 docker5:h1 20 进行路由计算。")
    print("通过 sudo python3 route_path.py docker2:h1 docker5:h1 20 进行路由部署。")


if __name__ == "__main__":
    if not os.path.exists(DOMAIN_SCRIPT_IN):
        print(f"错误: 找不到 {DOMAIN_SCRIPT_IN}")
        sys.exit(1)
    main(parse_args())
