#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import time
import os

def run_command(cmd):
    """运行 shell 命令并实时打印输出"""
    print(f"Exec: {cmd}")
    try:
        # 使用 check_call 确保如果有脚本报错，流程会感知（或者你可以改为 run 允许失败继续）
        subprocess.check_call(cmd, shell=True)
        return True
    except subprocess.CalledProcessError:
        print(f"[Error] Command failed: {cmd}")
        return False

def process_batch_file(filepath):
    if not os.path.exists(filepath):
        print(f"Error: File {filepath} not found.")
        sys.exit(1)

    print("="*50)
    print(f"Batch Processing Started: {filepath}")
    print("="*50)

    success_count = 0
    fail_count = 0
    line_num = 0

    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            # 跳过空行和注释
            if not line or line.startswith("#"):
                continue
            
            line_num += 1
            parts = line.split()
            
            # 格式检查: src dst bw
            if len(parts) < 3:
                print(f"\n[Skipping] Line {line_num}: Format error (expected: src dst bw)")
                continue

            src, dst, bw_str = parts[0], parts[1], parts[2]
            
            print(f"\n>>> [Flow {line_num}] Processing request: {src} -> {dst} (BW={bw_str}Mbps)")
            
            # 1. 计算路径 (Route Calculation)
            cmd_cal = f"sudo python3 route_cal.py {src} {dst} {bw_str}"
            if not run_command(cmd_cal):
                print(f"    [Fail] Calculation failed for Flow {line_num}")
                fail_count += 1
                continue

            # 2. 部署路径 (Route Deployment)
            # 默认使用 index 0 (最优路径)
            cmd_path = f"sudo python3 route_path.py {src} {dst} {bw_str} --index 0"
            if run_command(cmd_path):
                print(f"    [Success] Flow {line_num} Deployed.")
                success_count += 1
            else:
                print(f"    [Fail] Deployment failed for Flow {line_num}")
                fail_count += 1
            
            # 可选：稍微停顿一下，让控制器/OVS状态稳定
            time.sleep(1)

    print("\n" + "="*50)
    print("Batch Processing Complete")
    print(f"Total Requests: {success_count + fail_count}")
    print(f"Success: {success_count}")
    print(f"Failed:  {fail_count}")
    print("="*50)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: sudo python3 route_batch.py <request_file>")
        print("Example: sudo python3 route_batch.py traffic.txt")
        sys.exit(1)

    start_time = time.time()
    process_batch_file(sys.argv[1])
    print(f"[Total Time] {time.time() - start_time:.2f} seconds")

