#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
使用Tkinter构建GUI，调后端脚本进行路由，Matplotlib绘制拓扑
"""

# ===================== 标准库导入 =====================
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import json
import os
import time
import subprocess  # 调用后端脚本
import re  # 解析节点格式
import threading
import queue
import shlex

# ===================== 第三方库导入 =====================
import networkx as nx  # 图论算法库（拓扑绘制）
import matplotlib  # 绘图库

matplotlib.use('TkAgg')  # 设置后端为TkAgg，使其能在Tkinter中嵌入
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.patches as mpatches
import numpy as np

# ===================== 全局配置常量 =====================
BASE_DIR = os.path.dirname(__file__)
TOPO_DATA_DIR = os.path.join(BASE_DIR, "topo_data")
TOPO_PATH_DIR = os.path.join(BASE_DIR, "topo_path")
INTER_DOMAIN_JSON = "inter_domain_db.json"  # 域间连接配置文件

# 颜色配置：为每个子域分配独特的标识色
DOMAIN_COLORS = {
    "docker1": "#E74C3C",  # 红色
    "docker2": "#3498DB",  # 蓝色
    "docker3": "#2ECC71",  # 绿色
    "docker4": "#F39C12",  # 橙色
    "docker5": "#9B59B6"  # 紫色
}

# 显示名称映射：将docker名称映射为中文子域名称
DOMAIN_DISPLAY_NAMES = {
    "docker1": "domain1", "docker2": "domain2", "docker3": "domain3",
    "docker4": "domain4", "docker5": "domain5"
}

# 业务流路径颜色池：用于区分不同业务流的路径高亮显示
FLOW_COLORS = ["#FFFF00", "#FF00FF", "#00FFFF", "#FF6347", "#87CEFA", "#DDA0DD"]

# 颜色代码到中文名称的映射，用于文本展示
COLOR_NAME_MAP = {
    "#FFFF00": "黄色", "#FF00FF": "洋红色", "#00FFFF": "青色",
    "#FF6347": "番茄红", "#87CEFA": "浅蓝色", "#DDA0DD": "梅红色",
    "#FFD700": "金色"
}


# ===================== 拓扑数据加载 =====================
def load_topo():
    """
    从JSON文件加载多域网络拓扑数据
    1. 加载每个docker容器的内部拓扑（交换机、主机、链路）
    2. 加载域间连接配置（边界网关互联）
    3. 统一节点命名格式：{容器名}-{节点名}

    Returns:
        tuple: (DOMAIN_TOPO_CONFIG, INTER_DOMAIN_EDGES)
            - DOMAIN_TOPO_CONFIG: 字典，键为容器名，值为该域的节点和边列表
            - INTER_DOMAIN_EDGES: 列表，存储域间链路的端点
    """
    domain_topo_config = {}  # 存储各域内部拓扑
    inter_domain_edges = []  # 存储域间链路

    # 优先读取 topo_data 目录，若不存在则回退到脚本目录（兼容旧用法）
    topo_dir = TOPO_DATA_DIR if os.path.isdir(TOPO_DATA_DIR) else BASE_DIR
    domain_files = sorted(
        [f for f in os.listdir(topo_dir) if f.endswith("_topo.json")]
    )
    if not domain_files:
        raise FileNotFoundError(f"未找到拓扑文件（目录: {topo_dir}）")

    # 遍历每个域的拓扑文件
    for domain_file in domain_files:
        container_name = domain_file.split("_")[0]  # 从文件名提取容器名（如docker1）
        file_path = os.path.join(topo_dir, domain_file)

        with open(file_path, 'r', encoding='utf-8') as f:
            topo = json.load(f)

        switches = topo["switches"]  # 获取交换机连接信息
        nodes = set()
        edges = set()

        # 解析每个交换机及其连接
        for sw_name, sw_conns in switches.items():
            sw = f"{container_name}-{sw_name}"  # 构造全局唯一标识符
            nodes.add(sw)
            for conn_name, conn_info in sw_conns.items():
                if conn_name == sw_name:  # 跳过自环连接
                    continue
                # 仅保留可用链路（bw>0），使故障链路在拓扑上实时消失
                if isinstance(conn_info, dict):
                    try:
                        bw_val = float(conn_info.get("bw", 1))
                        if bw_val <= 0:
                            continue
                    except Exception:
                        pass
                conn = f"{container_name}-{conn_name}"
                nodes.add(conn)
                # 使用排序后的元组确保边的唯一性（无向图）
                edges.add(tuple(sorted([sw, conn])))

        # 存储该域的拓扑信息
        domain_topo_config[container_name] = {
            "nodes": sorted(nodes),
            "edges": [list(e) for e in sorted(edges)]
        }

    # 加载域间链路配置（边界网关之间的连接）
    inter_path = os.path.join(topo_dir, INTER_DOMAIN_JSON)
    if os.path.exists(inter_path):
        with open(inter_path, 'r', encoding='utf-8') as f:
            for conn in json.load(f):
                try:
                    # 仅保留可用域间链路（qos.bw>0）
                    qos = conn.get("qos", {})
                    try:
                        if float(qos.get("bw", 1)) <= 0:
                            continue
                    except Exception:
                        pass
                    # 构造域间网关节点名称：容器名-EXT_接口名
                    src = f"{conn['src_container']}-EXT_{conn['src_iface']}"
                    dst = f"{conn['dst_container']}-EXT_{conn['dst_iface']}"
                    inter_domain_edges.append([src, dst])
                except:
                    # 忽略格式错误的配置项
                    continue

    return domain_topo_config, inter_domain_edges


# 全局变量存储拓扑数据（由 GUI 在 draw 时自动刷新）
DOMAIN_TOPO_CONFIG = {}
INTER_DOMAIN_EDGES = []


# ===================== 后端脚本调用工具函数 =====================
def call_backend_script(script_name, src, dst, qos, extra_args=None):
    """
    调用后端Python脚本，返回JSON结果
    Args:
        script_name: 脚本名（如route_cal.py）
        src: 源节点（格式：docker2:h1）
        dst: 目的节点（格式：docker5:h1）
        qos: QoS值（整数）

    Returns:
        str: 后端脚本标准输出
    """
    try:
        if extra_args is None:
            extra_args = []

        # 构造执行命令：若当前已是root则直接执行，避免重复sudo
        cmd = ["python3", script_name, src, dst, str(qos)] + [str(a) for a in extra_args]
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            # 非 root 情况使用 non-interactive sudo，避免GUI卡在密码输入
            cmd = ["sudo", "-n"] + cmd

        # 执行脚本并捕获输出
        result = subprocess.run(
            cmd,
            cwd=BASE_DIR,  # 脚本所在目录
            capture_output=True,
            text=True,
            timeout=60  # 超时时间60秒
        )

        # 检查执行状态
        if result.returncode != 0:
            raise Exception(f"脚本执行失败：{result.stderr or result.stdout}")

        return result.stdout

    except subprocess.TimeoutExpired:
        raise Exception(f"调用{script_name}超时（60秒）")
    except Exception as e:
        raise Exception(f"调用后端失败：{str(e)}")


def build_path_filename(src, dst, qos):
    """
    route_cal.py 保存路径文件的命名规则。
    """
    safe_src = src.replace(':', '_')
    safe_dst = dst.replace(':', '_')
    p1, p2 = sorted([safe_src, safe_dst])
    return f"{p1}_{p2}_{int(float(qos))}.json"


def load_route_candidates(src, dst, qos):
    """
    从 topo_path 读取 route_cal.py 生成的候选路径。
    """
    filename = build_path_filename(src, dst, qos)
    path = os.path.join(TOPO_PATH_DIR, filename)
    if not os.path.exists(path):
        raise FileNotFoundError(f"未找到路径文件: {path}")
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, list) or len(data) == 0:
        raise ValueError(f"路径文件无有效候选: {path}")
    return data


def extract_exec_time(stdout_text):
    """
    从脚本输出中提取 [Time] Total Execution Time。
    """
    m = re.search(r"Total Execution Time:\s*([0-9.]+)\s*seconds", stdout_text)
    if m:
        return float(m.group(1))
    return None


def parse_backend_path(backend_path_data, inter_domain_edges=None):
    """
    解析后端返回的路径数据，转换为可视化的节点列表
    后端路径格式：
      分段容器内路径 + egress_iface + 域间连接
    转换为全局节点名（dockerX-节点名），并尽量补全 EXT 出接口节点。
    Args:
        backend_path_data: 后端返回的路径数据（含segments）
        inter_domain_edges: 可选，域间链路列表（用于补齐跨域对端 EXT 节点）

    Returns:
        list: 全局节点名列表（如["docker2-h1", "docker2-s1", ...]）
    """
    segments = backend_path_data.get("segments", [])
    if inter_domain_edges is None:
        inter_domain_edges = INTER_DOMAIN_EDGES

    cleaned_path = []

    def _append_node(node_name):
        if not node_name:
            return
        if not cleaned_path or cleaned_path[-1] != node_name:
            cleaned_path.append(node_name)

    for idx, segment in enumerate(segments):
        container = segment.get("container")
        seg_path = segment.get("path", [])
        if not container:
            continue

        # 1) 先添加该域内节点路径
        for node in seg_path:
            if node:
                _append_node(f"{container}-{node}")

        # 2) 非最后分段，补齐当前域出接口 + 下一域入接口(若可匹配)
        if idx < len(segments) - 1:
            egress_iface = segment.get("egress_iface")
            if egress_iface:
                egress_tag = str(egress_iface)
                if not egress_tag.startswith("EXT_"):
                    egress_tag = f"EXT_{egress_tag}"
                curr_ext = f"{container}-{egress_tag}"
                _append_node(curr_ext)

                next_container = segments[idx + 1].get("container")
                if next_container and inter_domain_edges:
                    prefix = f"{next_container}-EXT_"
                    for u, v in inter_domain_edges:
                        if u == curr_ext and isinstance(v, str) and v.startswith(prefix):
                            _append_node(v)
                            break
                        if v == curr_ext and isinstance(u, str) and u.startswith(prefix):
                            _append_node(u)
                            break

    return cleaned_path


# ===================== GUI主类 =====================
class SDN_GUI(tk.Tk):
    """
    可视化主窗口类
    继承自Tkinter的Tk类，提供图形界面
    """

    def __init__(self):
        """初始化GUI应用程序"""
        super().__init__()
        self.title("多域网络路由模型")  # 窗口标题
        self.geometry("1920x1080")  # 设置窗口尺寸为全高清

        # 业务流数据存储
        self.flows = {}  # 存储业务流信息：{flow_id: {'src':..., 'dst':..., 'qos':..., 'path':..., 'alternative_paths':...}}
        self.flow_colors = {}  # 存储每个业务流的颜色映射
        self.flow_seq = 1  # 自动业务流编号

        # 网络故障模拟状态
        self.deleted_nodes = set()  # 已删除（故障）的节点集合
        self.deleted_edges = set()  # 已删除（故障）的链路集合（存储排序后的元组）

        # 拓扑数据缓存（每次 draw 会刷新）
        self.domain_topo_config = {}
        self.inter_domain_edges = []

        # 性能监控变量（使用Tkinter的StringVar实现界面自动更新）
        self.route_gen_time = tk.StringVar(value="0.000")  # 路由生成耗时
        self.route_switch_time = tk.StringVar(value="0.000")  # 路由切换耗时
        self.monitor_interval = tk.StringVar(value="5.0")

        # 后台任务与日志
        self.log_queue = queue.Queue()
        self.bg_processes = {}
        self._last_runtime_sig = None
        self._topo_monitor_reroute_mode = False

        # 构建用户界面
        self._build_ui()
        self._reload_topology_data(silent=False)
        self._sync_flows_from_active_db()
        # 初始绘制拓扑图
        self.draw()
        self.after(120, self._flush_log_queue)
        self.after(1000, self._auto_refresh_runtime)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _reload_topology_data(self, silent=True):
        """
        刷新拓扑缓存，默认静默失败（避免频繁弹窗）。
        """
        global DOMAIN_TOPO_CONFIG, INTER_DOMAIN_EDGES
        try:
            DOMAIN_TOPO_CONFIG, INTER_DOMAIN_EDGES = load_topo()
            self.domain_topo_config = DOMAIN_TOPO_CONFIG
            self.inter_domain_edges = INTER_DOMAIN_EDGES
            return True
        except Exception as e:
            if not silent:
                messagebox.showerror("拓扑加载失败", str(e))
            self.domain_topo_config = {}
            self.inter_domain_edges = []
            return False

    def _runtime_data_files(self):
        """返回用于实时刷新的关键数据文件列表。"""
        files = []
        topo_dir = TOPO_DATA_DIR if os.path.isdir(TOPO_DATA_DIR) else BASE_DIR
        if not os.path.isdir(topo_dir):
            return files
        for name in sorted(os.listdir(topo_dir)):
            if name.endswith("_topo.json") or name in ("inter_domain_db.json", "active_flows.json"):
                files.append(os.path.join(topo_dir, name))
        return files

    def _runtime_signature(self):
        """
        计算运行时数据签名（基于文件mtime+size）。
        当拓扑或活动流文件变化时，签名会变化。
        """
        sig = []
        for path in self._runtime_data_files():
            try:
                st = os.stat(path)
                sig.append((path, st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                continue
        return tuple(sig)

    def _sync_flows_from_active_db(self):
        """
        从 topo_data/active_flows.json 同步业务流到前端内存。
        用于展示 monitor 重路由后的最新路径。
        """
        active_file = os.path.join(TOPO_DATA_DIR, "active_flows.json")
        if not os.path.exists(active_file):
            self.flows = {}
            return

        try:
            with open(active_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        if isinstance(data, dict):
            records = list(data.values())
        elif isinstance(data, list):
            records = data
        else:
            records = []

        new_flows = {}
        for rec in records:
            if not isinstance(rec, dict):
                continue
            flow_key = rec.get("flow_key")
            src = rec.get("src")
            dst = rec.get("dst")
            bw = rec.get("bw", 0)
            route = rec.get("route", {})
            if not flow_key or not src or not dst or not isinstance(route, dict):
                continue

            try:
                path_nodes = parse_backend_path(route, self.inter_domain_edges)
            except Exception:
                continue
            if len(path_nodes) < 2:
                continue

            src_show = str(src).replace("docker", "domain").replace(":", "-")
            dst_show = str(dst).replace("docker", "domain").replace(":", "-")
            new_flows[flow_key] = {
                "src": src_show,
                "dst": dst_show,
                "qos": bw,
                "path": path_nodes,
                "alternative_paths": [],
            }
            if flow_key not in self.flow_colors:
                self.flow_colors[flow_key] = FLOW_COLORS[len(self.flow_colors) % len(FLOW_COLORS)]

        self.flows = new_flows

    def _refresh_runtime_state(self):
        """
        检测运行时文件变化并刷新拓扑与业务流显示。
        """
        sig = self._runtime_signature()
        if sig == self._last_runtime_sig:
            return
        self._last_runtime_sig = sig
        self._reload_topology_data(silent=True)
        self._sync_flows_from_active_db()
        self.draw()

    def _auto_refresh_runtime(self):
        """
        周期性刷新运行时状态，实现拓扑与路径的实时更新。
        """
        try:
            self._refresh_runtime_state()
        finally:
            if self.winfo_exists():
                self.after(1000, self._auto_refresh_runtime)

    def _build_ui(self):
        """
        用户界面布局
        采用左右分栏布局：
        - 左侧：控制面板（280px宽），包含操作按钮和性能统计
        - 右侧：拓扑视图和路径信息展示
        """
        # 创建水平分割面板，允许调整左右面板宽度
        main = tk.PanedWindow(self, orient=tk.HORIZONTAL, sashwidth=4)
        main.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)

        # ===================== 左侧面板：控制区 =====================
        left = tk.Frame(main, width=280, bg='#f5f5f5')  # 浅灰色背景
        left.pack_propagate(False)  # 禁止子组件改变父容器大小
        main.add(left)

        # 面板标题
        tk.Label(left, text="控制台", font=('Microsoft YaHei', 16, 'bold'), bg='#f5f5f5').pack(pady=10)

        # 功能按钮：批量部署业务流（深绿）
        tk.Button(left, text="批量部署业务流", font=('Microsoft YaHei', 12, 'bold'),
                  bg='#1e8449', fg='white', height=2, command=self.batch_deploy_flows).pack(fill=tk.X, padx=10, pady=5)

        # 功能按钮：新增业务流（绿色）
        tk.Button(left, text="+ 新增业务流", font=('Microsoft YaHei', 12, 'bold'),
                  bg='#27ae60', fg='white', height=2, command=self.add_flow).pack(fill=tk.X, padx=10, pady=5)

        # 功能按钮：连通性测试（蓝绿色）
        tk.Button(left, text="连通性测试", font=('Microsoft YaHei', 12, 'bold'),
                  bg='#16a085', fg='white', height=2, command=self.ping_test).pack(fill=tk.X, padx=10, pady=5)

        # 功能按钮：拓扑检测（可开始/暂停）
        self.monitor_btn = tk.Button(
            left, text="开始拓扑检测", font=('Microsoft YaHei', 12, 'bold'),
            bg='#f39c12', fg='white', height=2, command=self.toggle_topology_monitor
        )
        self.monitor_btn.pack(fill=tk.X, padx=10, pady=5)
        interval_row = tk.Frame(left, bg='#f5f5f5')
        interval_row.pack(fill=tk.X, padx=10, pady=(0, 5))
        tk.Label(interval_row, text="检测周期(s):", bg='#f5f5f5', font=('Microsoft YaHei', 10)).pack(side=tk.LEFT)
        tk.Entry(interval_row, textvariable=self.monitor_interval, width=8, font=('Consolas', 10)).pack(side=tk.RIGHT)

        # 功能按钮：链路节点失效（红色）
        tk.Button(left, text="链路节点失效", font=('Microsoft YaHei', 12, 'bold'),
                  bg='#e74c3c', fg='white', height=2, command=self.del_link).pack(fill=tk.X, padx=10, pady=5)

        # 性能统计区域
        frm = tk.LabelFrame(left, text=" 性能统计 ", font=('Microsoft YaHei', 11, 'bold'), bg='#f5f5f5')
        frm.pack(fill=tk.X, padx=10, pady=20)

        # 路由生成时间显示
        tk.Label(frm, text="路由生成时间(秒):", bg='#f5f5f5', font=('Microsoft YaHei', 10)).pack(anchor='w', padx=5,
                                                                                                 pady=(5, 0))
        tk.Label(frm, textvariable=self.route_gen_time, font=('Microsoft YaHei', 16, 'bold'),
                 fg='#e74c3c', bg='#f5f5f5').pack(anchor='w', padx=5)

        # 路由切换时间显示
        tk.Label(frm, text="路由切换时间(秒):", bg='#f5f5f5', font=('Microsoft YaHei', 10)).pack(anchor='w', padx=5,
                                                                                                 pady=(10, 0))
        tk.Label(frm, textvariable=self.route_switch_time, font=('Microsoft YaHei', 16, 'bold'),
                 fg='#9b59b6', bg='#f5f5f5').pack(anchor='w', padx=5)

        # 拓扑检测信息区域（实时显示 topo_monitor 输出）
        monitor_frm = tk.LabelFrame(left, text=" 拓扑检测信息 ", font=('Microsoft YaHei', 10, 'bold'), bg='#f5f5f5')
        monitor_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.monitor_txt = scrolledtext.ScrolledText(
            monitor_frm, font=('Consolas', 9), height=14, wrap=tk.WORD
        )
        self.monitor_txt.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        self.monitor_txt.insert(1.0, "等待启动拓扑检测...\n")

        # ===================== 右侧面板：显示区 =====================
        right = tk.Frame(main)
        main.add(right, minsize=1300)  # 最小宽度1300px

        # 右侧可拖动分割：上拓扑，下终端
        right_split = tk.PanedWindow(right, orient=tk.VERTICAL, sashwidth=6)
        right_split.pack(fill=tk.BOTH, expand=True)

        # 拓扑可视化区域（上方）
        topo_frm = tk.LabelFrame(right_split, text=" 网络拓扑视图 ",
                                 font=('Microsoft YaHei', 12, 'bold'))
        right_split.add(topo_frm, minsize=420)

        # 创建Matplotlib图形
        self.fig = plt.Figure(figsize=(16, 9), dpi=100, facecolor='white')
        self.ax = self.fig.add_subplot(111)  # 单个子图
        self.canvas = FigureCanvasTkAgg(self.fig, master=topo_frm)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # 终端输出区域（下方）
        path_frm = tk.LabelFrame(right_split, text=" 终端 ", font=('Microsoft YaHei', 11, 'bold'))
        right_split.add(path_frm, minsize=120)

        # 带滚动条的文本框显示命令输出
        self.terminal_txt = scrolledtext.ScrolledText(path_frm, font=('Consolas', 10), height=9, wrap=tk.WORD)
        self.terminal_txt.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)

        # 文本操作按钮行
        btn_row = tk.Frame(path_frm)
        btn_row.pack(fill=tk.X, padx=3, pady=2)
        tk.Button(btn_row, text="清空", command=self.clear_terminal).pack(side=tk.RIGHT, padx=2)
        tk.Button(btn_row, text="导出", command=self.export_terminal).pack(side=tk.RIGHT, padx=2)

        # 初始提示文本
        self.terminal_txt.insert(1.0, "就绪。\n")

    def _append_text(self, widget, text):
        """向文本框追加文本并自动滚动到底部。"""
        try:
            widget.insert(tk.END, text)
            widget.see(tk.END)
        except tk.TclError:
            return

    def _flush_log_queue(self):
        """将后台线程日志刷到UI控件（主线程执行）。"""
        try:
            while True:
                target, text = self.log_queue.get_nowait()
                if target == "monitor":
                    self._append_text(self.monitor_txt, text)
                elif target == "both":
                    self._append_text(self.monitor_txt, text)
                    self._append_text(self.terminal_txt, text)
                elif target == "__monitor_stopped__":
                    self._set_monitor_button(False)
                elif target == "__reroute_avg__":
                    route_avg, switch_avg = text
                    self.route_gen_time.set(f"{route_avg:.4f}")
                    self.route_switch_time.set(f"{switch_avg:.4f}")
                else:
                    self._append_text(self.terminal_txt, text)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._flush_log_queue)

    def _log_terminal(self, text):
        self.log_queue.put(("terminal", text))

    def _log_monitor(self, text):
        self.log_queue.put(("monitor", text))

    def _build_python_cmd(self, script_name, args=None, use_sudo=True):
        """
        构造 Python 脚本命令。
        非 root 场景下使用 sudo -n，避免GUI被密码提示阻塞。
        """
        if args is None:
            args = []
        cmd = ["python3", script_name] + [str(a) for a in args]
        if use_sudo and hasattr(os, "geteuid") and os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        return cmd

    def _display_python_cmd(self, script_name, args=None):
        """用于展示的命令字符串，固定显示为 sudo python3 ...。"""
        if args is None:
            args = []
        cmd = ["sudo", "python3", script_name] + [str(a) for a in args]
        return self._format_cmd_for_log(cmd)

    def _build_cmd_with_sudo(self, base_cmd):
        """为任意命令按需增加 sudo -n 前缀。"""
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            return ["sudo", "-n"] + list(base_cmd)
        return list(base_cmd)

    def _format_cmd_for_log(self, cmd):
        return " ".join(shlex.quote(x) for x in cmd)

    def _candidate_path_lines(self, route_result, max_count=5):
        """把候选路径转成精简可读文本。"""
        lines = []
        for idx, item in enumerate(route_result[:max_count]):
            try:
                score = float(item.get("score", 0))
            except Exception:
                score = 0.0
            try:
                p_nodes = parse_backend_path(item, self.inter_domain_edges)
                p_show = " -> ".join(n.replace("docker", "domain") for n in p_nodes)
            except Exception:
                p_show = "(path parse failed)"
            lines.append(f"  [{idx}] Score={score:.2f}")
            lines.append(f"      Path: {p_show}")
        return lines

    def _deploy_bw_lines(self, deploy_stdout):
        """提取部署输出中的带宽变更行。"""
        out = []
        for raw in (deploy_stdout or "").splitlines():
            line = raw.strip()
            if line.startswith("[Inter]") or line.startswith("[Intra "):
                out.append(f"  {line}")
        return out

    def _parse_reroute_avg_line(self, line):
        """
        解析 topo_monitor 输出的平均重路由耗时：
        [REROUTE AVG] route_gen=0.1234s route_switch=0.2345s flows=3
        """
        m = re.search(
            r"\[REROUTE AVG\]\s*route_gen=([0-9.]+)s\s*route_switch=([0-9.]+)s",
            line or "",
        )
        if not m:
            return None
        return float(m.group(1)), float(m.group(2))

    def _classify_topo_monitor_line(self, line):
        """
        拓扑检测输出分流：
        - 检测/故障识别信息 -> monitor
        - 重路由与重部署信息 -> terminal
        """
        monitor_cycle_head = re.search(
            r"^\[\d{2}:\d{2}:\d{2}\]\s+Monitoring\s+(Inter|Intra)-Domain\s+Topology\.\.\.",
            line or "",
        )
        if monitor_cycle_head:
            self._topo_monitor_reroute_mode = False
            return "monitor"

        reroute_start_keywords = [
            "Impacted active flows",
            "No active flow is affected.",
            "Releasing old reservation",
            ">>> Re-routing",
            "Trying cached backup candidates",
            "[Backup]",
            "Re-route deployed",
            "Backup route deployed",
            "[REROUTE]",
            "[REROUTE AVG]",
            "[Fail] route_cal",
            "[Fail] route_path",
        ]
        for kw in reroute_start_keywords:
            if kw in line:
                self._topo_monitor_reroute_mode = True
                return "terminal"

        # 进入重路由阶段后，直到下一个 Monitoring 周期头，整段都写到终端。
        if self._topo_monitor_reroute_mode:
            return "terminal"

        return "monitor"

    def _stream_process_output(self, name, proc, target):
        """后台读取子进程输出并写入日志队列。"""
        try:
            if proc.stdout is not None:
                for line in iter(proc.stdout.readline, ''):
                    if not line:
                        break
                    if name == "topo_monitor" and target == "monitor":
                        avg_pair = self._parse_reroute_avg_line(line)
                        if avg_pair is not None:
                            self.log_queue.put(("__reroute_avg__", avg_pair))
                        line_target = self._classify_topo_monitor_line(line)
                        self.log_queue.put((line_target, line))
                    else:
                        self.log_queue.put((target, line))
            rc = proc.wait()
            end_msg = f"[{name}] exited with code {rc}\n"
            self.log_queue.put((target, end_msg))
        except Exception as e:
            self.log_queue.put((target, f"[{name}] output error: {e}\n"))
        finally:
            self.bg_processes.pop(name, None)
            if name == "topo_monitor":
                self.log_queue.put(("__monitor_stopped__", ""))

    def _terminate_bg_process(self, name):
        info = self.bg_processes.get(name)
        if not info:
            return
        proc = info.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.bg_processes.pop(name, None)

    def _launch_background_cmd(self, name, cmd, target="terminal", restart=False):
        """
        启动后台命令并实时收集日志。
        target:
            - terminal: 仅写终端框
            - monitor: 仅写拓扑检测框
            - both: 同时写两处
        """
        running = self.bg_processes.get(name)
        if running and running["proc"].poll() is None:
            if not restart:
                self._log_terminal(f"[{name}] already running.\n")
                return False
            self._terminate_bg_process(name)

        cmd_line = f"$ {self._format_cmd_for_log(cmd)}\n"
        if target == "monitor":
            self._log_monitor(cmd_line)
        else:
            self._log_terminal(cmd_line)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            if target == "monitor":
                self._log_monitor(f"[{name}] start failed: {e}\n")
            else:
                self._log_terminal(f"[{name}] start failed: {e}\n")
            return False

        th = threading.Thread(
            target=self._stream_process_output, args=(name, proc, target), daemon=True
        )
        th.start()
        self.bg_processes[name] = {"proc": proc, "thread": th}
        return True

    def _run_cmd_capture(self, cmd, timeout=30):
        """同步执行短命令并返回 (ok, output)。"""
        try:
            result = subprocess.run(
                cmd,
                cwd=BASE_DIR,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, f"[timeout] command exceeded {timeout}s\n"
        except Exception as e:
            return False, str(e)

    def _normalize_endpoint(self, value):
        """
        UI输入统一为后端格式 dockerX:hY。
        支持:
          - domain1-h1
          - docker1-h1
          - domain1:h1
          - docker1:h1
        """
        s = value.strip()
        if not s:
            raise ValueError("节点输入不能为空")

        s = s.replace("domain", "docker")
        if "-" in s and ":" not in s:
            left, right = s.split("-", 1)
            s = f"{left}:{right}"
        if ":" not in s:
            raise ValueError(f"节点格式错误: {value}")

        c, n = s.split(":", 1)
        if not c.startswith("docker") or not n:
            raise ValueError(f"节点格式错误: {value}")
        return f"{c}:{n}"

    def _normalize_host_endpoint(self, value):
        """规范化主机节点输入（必须是 hX）。"""
        endpoint = self._normalize_endpoint(value)
        _, node = endpoint.split(":", 1)
        if not node.startswith("h"):
            raise ValueError(f"主机格式错误（需要 hX）: {value}")
        return endpoint

    def _lookup_host_ip(self, endpoint):
        """从 topo_data 读取主机 IP。"""
        container, host = endpoint.split(":", 1)
        topo_file = os.path.join(TOPO_DATA_DIR, f"{container}_topo.json")
        if not os.path.exists(topo_file):
            topo_file = os.path.join(BASE_DIR, f"{container}_topo.json")
        if not os.path.exists(topo_file):
            raise FileNotFoundError(f"未找到拓扑文件: {container}_topo.json")
        with open(topo_file, "r", encoding="utf-8") as f:
            topo = json.load(f)
        hosts = topo.get("hosts", {})
        if host not in hosts:
            raise ValueError(f"{container} 中不存在主机 {host}")
        ip = hosts[host].get("ip", "").strip()
        if not ip:
            raise ValueError(f"{container}:{host} 未配置 IP")
        return ip

    def ping_test(self):
        """连通性测试：源主机 ping 目的主机。"""
        win = tk.Toplevel(self)
        win.title("连通性测试")
        win.geometry("520x320")

        tk.Label(win, text="源主机 (domain1-h1 / docker1:h1):", font=('Microsoft YaHei', 11)).pack(anchor='w', padx=10, pady=3)
        esrc = tk.Entry(win, font=('Consolas', 10))
        esrc.pack(fill=tk.X, padx=10)
        esrc.insert(0, "domain1-h1")

        tk.Label(win, text="目的主机 (domain5-h1 / docker5:h1):", font=('Microsoft YaHei', 11)).pack(anchor='w', padx=10, pady=3)
        edst = tk.Entry(win, font=('Consolas', 10))
        edst.pack(fill=tk.X, padx=10)
        edst.insert(0, "domain5-h1")

        row = tk.Frame(win)
        row.pack(fill=tk.X, padx=10, pady=6)
        tk.Label(row, text="次数:", font=('Microsoft YaHei', 10)).pack(side=tk.LEFT)
        ecount = tk.Entry(row, width=6, font=('Consolas', 10))
        ecount.pack(side=tk.LEFT, padx=5)
        ecount.insert(0, "3")
        tk.Label(row, text="超时(s):", font=('Microsoft YaHei', 10)).pack(side=tk.LEFT, padx=(15, 0))
        ew = tk.Entry(row, width=6, font=('Consolas', 10))
        ew.pack(side=tk.LEFT, padx=5)
        ew.insert(0, "1")

        result_var = tk.StringVar(value="等待输入...")
        tk.Label(win, textvariable=result_var, font=('Microsoft YaHei', 10), fg='#16a085').pack(pady=10)

        def run_ping():
            try:
                src = self._normalize_host_endpoint(esrc.get().strip())
                dst = self._normalize_host_endpoint(edst.get().strip())
                count = int(ecount.get().strip())
                timeout_s = int(ew.get().strip())
                if count <= 0 or timeout_s <= 0:
                    raise ValueError("次数和超时必须大于0")

                src_c, src_h = src.split(":", 1)
                dst_ip = self._lookup_host_ip(dst)
                src_show = src.replace("docker", "domain").replace(":", "-")
                dst_show = dst.replace("docker", "domain").replace(":", "-")

                cmd = self._build_cmd_with_sudo(
                    ["docker", "exec", src_c, "m", src_h, "ping", "-c", str(count), "-W", str(timeout_s), dst_ip]
                )
                self._log_terminal(f"\n>>> [PING] {src_show} -> {dst_show} ({dst_ip})\n")
                self._log_terminal(f"$ {self._format_cmd_for_log(cmd)}\n")

                ok, output = self._run_cmd_capture(cmd, timeout=max(20, count * (timeout_s + 3)))
                if output:
                    self._log_terminal(output if output.endswith("\n") else output + "\n")

                if ok:
                    result_var.set("连通性测试完成")
                else:
                    result_var.set("连通性测试失败")
            except Exception as e:
                result_var.set(f"错误: {str(e)}")
                self._log_terminal(f"[ping-test] failed: {str(e)}\n")

        tk.Button(win, text="开始测试", command=run_ping, bg='#16a085', fg='white',
                  font=('Microsoft YaHei', 11, 'bold')).pack(pady=8, fill=tk.X, padx=50)

    def _is_bg_running(self, name):
        info = self.bg_processes.get(name)
        if not info:
            return False
        proc = info.get("proc")
        return proc is not None and proc.poll() is None

    def _set_monitor_button(self, running):
        if running:
            self.monitor_btn.config(text="暂停拓扑检测", bg="#c0392b")
        else:
            self.monitor_btn.config(text="开始拓扑检测", bg="#f39c12")

    def toggle_topology_monitor(self):
        """切换拓扑检测状态：开始 / 暂停。"""
        if self._is_bg_running("topo_monitor"):
            self.stop_topology_monitor()
        else:
            self.start_topology_monitor()

    def stop_topology_monitor(self):
        """暂停拓扑检测。"""
        self._topo_monitor_reroute_mode = False
        if not self._is_bg_running("topo_monitor"):
            self._set_monitor_button(False)
            self._log_monitor(">>> 拓扑检测当前未运行\n")
            return
        self._terminate_bg_process("topo_monitor")
        self._set_monitor_button(False)
        self._log_monitor(">>> 已暂停拓扑检测\n")

    def start_topology_monitor(self):
        """后台启动拓扑检测脚本并实时显示检测信息。"""
        self._topo_monitor_reroute_mode = False
        interval = self.monitor_interval.get().strip() or "5.0"
        try:
            interval_f = float(interval)
            if interval_f <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("输入错误", "监测周期必须是大于0的数字。")
            return

        # 使用 -u 关闭 Python 缓冲，确保每个检测周期日志实时显示
        cmd = ["python3", "-u", "topo_monitor.py", "--interval", str(interval_f)]
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        ok = self._launch_background_cmd(
            "topo_monitor", cmd, target="monitor", restart=False
        )
        if ok:
            self._set_monitor_button(True)
            self._log_monitor(f"\n>>> 启动拓扑检测，interval={interval_f}s\n")
        else:
            self._set_monitor_button(False)
            if hasattr(os, "geteuid") and os.geteuid() != 0:
                self._log_terminal("[hint] 请用 sudo 启动 UI 或配置免密 sudo。\n")

    def _on_close(self):
        """关闭窗口前清理后台进程。"""
        for name in list(self.bg_processes.keys()):
            self._terminate_bg_process(name)
        self.destroy()

    def get_positions(self):
        """
        计算网络拓扑中各节点的布局位置。
        布局策略：域级布局 + 域内布局，确保每个 docker 内节点聚集显示。

        Returns:
            dict: 节点到坐标位置的映射 {node: (x, y), ...}
        """
        full_graph = nx.Graph()
        self._reload_topology_data(silent=True)

        # 构建图结构，排除已删除的节点和链路
        for domain, cfg in self.domain_topo_config.items():
            for edge in cfg['edges']:
                u, v = edge
                # 跳过已删除的节点
                if u not in self.deleted_nodes and v not in self.deleted_nodes:
                    edge_key = tuple(sorted([u, v]))
                    # 跳过已删除的链路（双向检查）
                    if edge_key not in self.deleted_edges and (v, u) not in self.deleted_edges:
                        full_graph.add_edge(u, v)

        # 添加域间链路
        for u, v in self.inter_domain_edges:
            if u not in self.deleted_nodes and v not in self.deleted_nodes:
                edge_key = tuple(sorted([u, v]))
                if edge_key not in self.deleted_edges and (v, u) not in self.deleted_edges:
                    full_graph.add_edge(u, v)

        # 空图保护
        if len(full_graph.nodes()) == 0:
            return {}

        # 1) 按 docker 域分组节点
        domain_nodes = {}
        for n in full_graph.nodes():
            d = n.split('-')[0]
            domain_nodes.setdefault(d, []).append(n)

        # 2) 计算域级中心位置（固定锚点，避免实时刷新时域间乱跑/重叠）
        def _domain_sort_key(name):
            m = re.match(r"docker(\d+)$", str(name))
            if m:
                return (0, int(m.group(1)))
            return (1, str(name))

        # 以预定义域列表为主，保证即使某域暂时无节点，其他域位置也不变
        anchor_domains = list(DOMAIN_COLORS.keys())
        for d in sorted(domain_nodes.keys(), key=_domain_sort_key):
            if d not in anchor_domains:
                anchor_domains.append(d)

        domain_centers = {}
        n_anchor = max(len(anchor_domains), 1)
        anchor_radius = 640.0 + max(0, n_anchor - 5) * 35.0
        start_angle = -np.pi / 2.0  # 从顶部开始，顺时针分布
        for i, d in enumerate(anchor_domains):
            theta = start_angle + (2.0 * np.pi * i / n_anchor)
            domain_centers[d] = np.array(
                [anchor_radius * np.cos(theta), anchor_radius * np.sin(theta)]
            )

        def _node_kind(name):
            tail = name.split('-')[-1]
            if 'EXT_' in name:
                return 'ext'
            if tail.startswith('s'):
                return 'switch'
            if tail.startswith('h'):
                return 'host'
            return 'other'

        def _min_sep(a, b):
            ta, tb = _node_kind(a), _node_kind(b)
            pair = tuple(sorted((ta, tb)))
            # 经验参数：以当前布局坐标系估算的最小间距
            sep_table = {
                ('ext', 'ext'): 110.0,
                ('ext', 'host'): 82.0,
                ('ext', 'switch'): 96.0,
                ('host', 'host'): 40.0,
                ('host', 'switch'): 58.0,
                ('switch', 'switch'): 84.0,
            }
            return sep_table.get(pair, 42.0)

        def _node_local_sort_key(name):
            tail = name.split('-')[-1]
            if tail.startswith('s'):
                m = re.match(r"s(\d+)", tail)
                return (0, int(m.group(1)) if m else 10**9, tail)
            if tail.startswith('h'):
                m = re.match(r"h(\d+)", tail)
                return (1, int(m.group(1)) if m else 10**9, tail)
            if 'EXT_' in name:
                return (2, 0, tail)
            return (3, 0, tail)

        def _resolve_collisions(local_pos, node_list, iterations=240):
            # 轻量级排斥迭代：保证节点最小间距，降低遮挡
            for _ in range(iterations):
                moved = 0.0
                for i in range(len(node_list)):
                    ni = node_list[i]
                    pi = local_pos[ni]
                    for j in range(i + 1, len(node_list)):
                        nj = node_list[j]
                        pj = local_pos[nj]
                        delta = pi - pj
                        dist = np.linalg.norm(delta)
                        need = _min_sep(ni, nj)
                        if dist < 1e-6:
                            # 完全重合时随机微扰
                            angle = (i * 37 + j * 53) * np.pi / 180.0
                            delta = np.array([np.cos(angle), np.sin(angle)])
                            dist = 1e-3
                        if dist < need:
                            push = (need - dist) * 0.5
                            vec = delta / dist
                            pi += vec * push
                            pj -= vec * push
                            local_pos[ni] = pi
                            local_pos[nj] = pj
                            moved += push
                # 轻微回拉，避免节点群无限外扩
                if moved > 0:
                    centroid = np.mean([local_pos[n] for n in node_list], axis=0)
                    for n in node_list:
                        local_pos[n] = local_pos[n] - centroid * 0.015
                if moved < 1e-3:
                    break

        # 3) 每个域内独立布局，再整体平移到该域中心
        # 域内布局策略：
        # - 交换机：域内骨架（spring）
        # - 主机：围绕其接入交换机分层环绕
        # - EXT网关：域外圈显示，避免压在核心区域
        pos = {}
        for d, nodes in domain_nodes.items():
            subg = full_graph.subgraph(nodes).copy()
            n_cnt = len(nodes)
            domain_radius = max(180.0, min(360.0, 31.0 * np.sqrt(max(n_cnt, 1))))
            switch_nodes = sorted(
                [n for n in nodes if n.split('-')[-1].startswith('s') and 'EXT_' not in n],
                key=_node_local_sort_key,
            )
            host_nodes = sorted(
                [n for n in nodes if n.split('-')[-1].startswith('h')],
                key=_node_local_sort_key,
            )
            ext_nodes = sorted(
                [n for n in nodes if 'EXT_' in n],
                key=_node_local_sort_key,
            )

            local_pos = {}

            # 3.1 交换机布局（域内骨架）
            if len(switch_nodes) == 1:
                local_pos[switch_nodes[0]] = np.array([0.0, 0.0])
            elif len(switch_nodes) > 1:
                sw_sub = subg.subgraph(switch_nodes).copy()
                # 放大交换机骨架，提升域内交换机间距
                sw_scale = domain_radius * 1.35
                sw_k = max(3.0, 8.0 / np.sqrt(len(switch_nodes)))
                sw_pos = nx.spring_layout(
                    sw_sub, seed=42, scale=sw_scale, k=sw_k, iterations=680
                )
                local_pos.update({n: np.array(p) for n, p in sw_pos.items()})

            # 3.2 主机布局（围绕接入交换机）
            hosts_by_switch = {}
            for h in host_nodes:
                nbr_sw = [x for x in subg.neighbors(h) if x in switch_nodes]
                if nbr_sw:
                    anchor = nbr_sw[0]
                elif switch_nodes:
                    anchor = switch_nodes[hash(h) % len(switch_nodes)]
                else:
                    anchor = None
                if anchor is None:
                    local_pos[h] = np.array([0.0, 0.0])
                else:
                    hosts_by_switch.setdefault(anchor, []).append(h)

            for anchor, hs in hosts_by_switch.items():
                base = local_pos.get(anchor, np.array([0.0, 0.0]))
                base_orbit = max(58.0, domain_radius * 0.52)
                # 每个交换机分配一个稳定角度偏移，避免域内同相位重叠
                phi0 = (abs(hash(anchor)) % 360) * np.pi / 180.0
                m = len(hs)
                ring_cap = 5
                for i, h in enumerate(sorted(hs, key=_node_local_sort_key)):
                    ring_idx = i // ring_cap
                    idx_in_ring = i % ring_cap
                    ring_size = min(ring_cap, m - ring_idx * ring_cap)
                    theta = phi0 + 2 * np.pi * idx_in_ring / max(ring_size, 1)
                    orbit = base_orbit + ring_idx * max(32.0, domain_radius * 0.18)
                    local_pos[h] = base + np.array(
                        [orbit * np.cos(theta), orbit * np.sin(theta)]
                    )

            # 3.3 EXT网关布局（域外圈）
            if ext_nodes:
                ext_r = domain_radius * 1.20
                m = len(ext_nodes)
                # 固定在上半圈，避免和主机环绕区重叠
                for i, e in enumerate(sorted(ext_nodes, key=_node_local_sort_key)):
                    theta = np.pi * (0.2 + 0.6 * (i + 0.5) / max(m, 1))
                    local_pos[e] = np.array([ext_r * np.cos(theta), ext_r * np.sin(theta)])

            # 3.4 兜底：任何遗漏节点放到外层环
            missing = sorted([n for n in nodes if n not in local_pos], key=_node_local_sort_key)
            if missing:
                miss_r = domain_radius * 1.05
                for i, n in enumerate(missing):
                    theta = 2 * np.pi * i / max(len(missing), 1)
                    local_pos[n] = np.array([miss_r * np.cos(theta), miss_r * np.sin(theta)])

            # 3.5 域内防遮挡迭代
            _resolve_collisions(local_pos, list(local_pos.keys()), iterations=260)

            center = domain_centers.get(d, np.array([0.0, 0.0]))
            for n, p in local_pos.items():
                pos[n] = np.array([p[0], p[1]]) + center

        return pos

    def draw(self):
        """
        绘制网络拓扑图到Matplotlib画布
        分层绘制顺序：背景边→域间链路→业务流高亮→节点→标签
        """
        self.ax.clear()  # 清空上一帧
        self.ax.set_facecolor('#fafafa')  # 设置浅灰背景
        self._reload_topology_data(silent=True)

        # 辅助函数：安全地为 matplotlib 对象设置层级 (zorder) 绕过 networkx 的传参限制
        def set_zorder(elements, z):
            if elements is None:
                return
            if isinstance(elements, list):
                for e in elements:
                    try: e.set_zorder(z)
                    except AttributeError: pass
            elif isinstance(elements, dict):
                for e in elements.values():
                    try: e.set_zorder(z)
                    except AttributeError: pass
            else:
                try: elements.set_zorder(z)
                except AttributeError: pass

        pos = self.get_positions()
        if not pos:
            self.canvas.draw()
            return

        nodes = list(pos.keys())

        # ===================== 节点分类 =====================
        # 根据节点命名规则分类：主机(h开头)、交换机(s开头)、外部网关(EXT_)
        hosts = [n for n in nodes if n.split('-')[-1].startswith('h')]
        switches = [n for n in nodes if n.split('-')[-1].startswith('s') and 'EXT_' not in n]
        exts = [n for n in nodes if 'EXT_' in n]  # 边界网关

        # ===================== 边分类 =====================
        switch_edges = []  # 交换机-交换机链路（骨干链路）
        access_edges = []  # 交换机-主机链路（接入链路）
        intra_other = []  # 其他内部链路

        # 分类处理各域内部链路
        for domain, cfg in self.domain_topo_config.items():
            for edge in cfg['edges']:
                u, v = edge
                if u not in nodes or v not in nodes:
                    continue
                edge_key = tuple(sorted([u, v]))
                if edge_key in self.deleted_edges or (v, u) in self.deleted_edges:
                    continue  # 跳过故障链路

                # 判断连接类型
                u_is_sw = u in switches
                v_is_sw = v in switches
                u_is_host = u in hosts
                v_is_host = v in hosts

                if u_is_sw and v_is_sw:
                    switch_edges.append((u, v))
                elif (u_is_sw and v_is_host) or (u_is_host and v_is_sw):
                    access_edges.append((u, v))
                else:
                    intra_other.append((u, v))

        # 处理域间链路（金色虚线显示）
        inter_edges = []
        for u, v in self.inter_domain_edges:
            if u in nodes and v in nodes:
                edge_key = tuple(sorted([u, v]))
                if edge_key not in self.deleted_edges and (v, u) not in self.deleted_edges:
                    inter_edges.append((u, v))

        # ===================== 提取业务流路径 =====================
        # 这里按“整条路径”绘制（而不是逐段单独粗线），视觉更连贯
        flow_paths = []  # [(fid, [node...], color), ...]
        for fid, info in self.flows.items():
            path = info.get('path', [])
            if not path:
                continue
            valid_path = [n for n in path if n in nodes]
            if len(valid_path) < 2:
                continue
            color = self.flow_colors.get(fid, '#FFD700')
            flow_paths.append((fid, valid_path, color))

        # ===================== 绘制边（按层次） =====================
        # 1. 交换机间骨干链路（深灰色，粗线）
        if switch_edges:
            edges = nx.draw_networkx_edges(
                nx.Graph(), pos, ax=self.ax,
                edgelist=switch_edges,
                edge_color='#555555', width=2.5, alpha=0.8
            )
            set_zorder(edges, 1)

        # 2. 接入链路（灰色，中等粗细）
        if access_edges:
            edges = nx.draw_networkx_edges(
                nx.Graph(), pos, ax=self.ax,
                edgelist=access_edges,
                edge_color='#888888', width=1.5, alpha=0.6
            )
            set_zorder(edges, 1)

        # 3. 其他内部链路（浅灰色，细线）
        if intra_other:
            edges = nx.draw_networkx_edges(
                nx.Graph(), pos, ax=self.ax,
                edgelist=intra_other,
                edge_color='#cccccc', width=0.8, alpha=0.5
            )
            set_zorder(edges, 1)

        # 4. 域间链路（金色，粗虚线，高层级）
        if inter_edges:
            edges = nx.draw_networkx_edges(
                nx.Graph(), pos, ax=self.ax,
                edgelist=inter_edges,
                edge_color='#DAA520', width=4, alpha=0.9,
                style='--'
            )
            set_zorder(edges, 3)

        # 5. 业务流路径高亮（双色分层：外层柔和描边 + 内层彩色主线）
        if flow_paths:
            for _, p_nodes, color in flow_paths:
                xs = [pos[n][0] for n in p_nodes]
                ys = [pos[n][1] for n in p_nodes]
                # 外层描边，让路径在复杂背景中更清晰
                self.ax.plot(
                    xs, ys,
                    color='white',
                    linewidth=6.2,
                    alpha=0.55,
                    solid_capstyle='round',
                    solid_joinstyle='round',
                    zorder=9,
                )
                # 内层主线，较细更精致
                self.ax.plot(
                    xs, ys,
                    color=color,
                    linewidth=3.0,
                    alpha=0.95,
                    solid_capstyle='round',
                    solid_joinstyle='round',
                    zorder=10,
                )

        # ===================== 绘制节点（按类型分层） =====================
        # 1. 主机节点（圆形，较小，按域着色）
        if hosts:
            by_domain = {}
            for n in hosts:
                d = n.split('-')[0]
                by_domain.setdefault(d, []).append(n)
            for domain, ns in by_domain.items():
                if ns:
                    nodes_col = nx.draw_networkx_nodes(
                        nx.Graph(), pos, ax=self.ax,
                        nodelist=ns,
                        node_color=DOMAIN_COLORS[domain],
                        node_size=400,  # 主机较小
                        alpha=0.9,
                        edgecolors='#2c3e50', linewidths=2
                    )
                    set_zorder(nodes_col, 4)

        # 2. 交换机节点（方形，较大，按域着色）
        if switches:
            by_domain = {}
            for n in switches:
                d = n.split('-')[0]
                by_domain.setdefault(d, []).append(n)
            for domain, ns in by_domain.items():
                if ns:
                    nodes_col = nx.draw_networkx_nodes(
                        nx.Graph(), pos, ax=self.ax,
                        nodelist=ns,
                        node_color=DOMAIN_COLORS[domain],
                        node_size=700,  # 交换机较大
                        alpha=1.0,
                        edgecolors='black', linewidths=2.5
                    )
                    set_zorder(nodes_col, 5)

        # 3. 边界网关（深灰色，最大，红色边框特殊标识）
        if exts:
            nodes_col = nx.draw_networkx_nodes(
                nx.Graph(), pos, ax=self.ax,
                nodelist=exts,
                node_color='#2c3e50',
                node_size=900,  # 网关最大
                alpha=1.0,
                edgecolors='#e74c3c', linewidths=3  # 红色边框突出显示
            )
            set_zorder(nodes_col, 6)

        # ===================== 绘制标签 =====================
        # 交换机标签（白色粗体，显示在节点中心）
        if switches:
            sw_labels = {n: n.split('-')[-1] for n in switches}  # 只显示短名称（如s1）
            labels = nx.draw_networkx_labels(
                nx.Graph(), pos, sw_labels, ax=self.ax,
                font_size=9, font_weight='bold', font_color='white'
            )
            set_zorder(labels, 7)

        # 主机标签（黑色，显示在节点旁）
        if hosts:
            h_labels = {n: n.split('-')[-1] for n in hosts}
            labels = nx.draw_networkx_labels(
                nx.Graph(), pos, h_labels, ax=self.ax,
                font_size=8, font_color='black', alpha=0.9
            )
            set_zorder(labels, 7)

        # 网关标签（带背景框，将EXT_eth0简写为E）
        if exts:
            ext_labels = {n: n.split('-')[-1].replace('EXT_', 'E') for n in exts}
            labels = nx.draw_networkx_labels(
                nx.Graph(), pos, ext_labels, ax=self.ax,
                font_size=8, font_weight='bold', font_color='white',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='black', alpha=0.7)
            )
            set_zorder(labels, 8)

        # ===================== 绘制子域图例（右侧垂直排列） =====================
        y_start = 0.95
        y_step = 0.06
        for i, domain in enumerate(['docker1', 'docker2', 'docker3', 'docker4', 'docker5']):
            if any(n.startswith(domain) for n in nodes):
                self.ax.text(0.98, y_start - i * y_step, DOMAIN_DISPLAY_NAMES[domain],
                             transform=self.ax.transAxes,
                             fontsize=12, fontweight='bold', ha='right', va='top',
                             bbox=dict(boxstyle='round,pad=0.5',
                                       facecolor=DOMAIN_COLORS[domain],
                                       edgecolor='black', alpha=0.9, linewidth=2),
                             zorder=20)

        # ===================== 添加图例（左上区域） =====================
        legend_elements = [
            mpatches.Patch(color='#DAA520', label='Inter-Domain'),  # 域间链路
            plt.Line2D([0], [0], color='#555555', lw=2.5, label='Switch-Switch'),  # 骨干链路
            plt.Line2D([0], [0], color='#888888', lw=1.5, label='Switch-Host'),  # 接入链路
            plt.Line2D([0], [0], marker='s', color='w', markerfacecolor='gray',
                       markersize=12, label='Switch'),  # 交换机
            plt.Line2D([0], [0], marker='o', color='w', markerfacecolor='gray',
                       markersize=8, label='Host'),  # 主机
            plt.Line2D([0], [0], marker='o', color='w', markerfacecolor='#2c3e50',
                       markeredgecolor='#e74c3c', markersize=14, markeredgewidth=2, label='Gateway')  # 网关
        ]

        # 将图例移动到左上区域（统计信息下方）以减少与拓扑遮挡
        legend = self.ax.legend(
            handles=legend_elements,
            loc='upper left',
            bbox_to_anchor=(0.01, 0.86),
            fontsize=9,
            frameon=True,
            fancybox=True,
            framealpha=0.95,
            ncol=1,
            borderpad=0.6,
            labelspacing=0.35,
        )
        try:
            legend.set_zorder(30)
        except Exception:
            pass

        # ===================== 添加统计信息（左上角） =====================
        # 这里使用英文，避免 Matplotlib 缺失中文字体时出现方框乱码
        stats = f"Nodes: {len(nodes)} | Inter-links: {len(inter_edges)} | Flows: {len(self.flows)}"
        self.ax.text(0.02, 0.98, stats, transform=self.ax.transAxes,
                     fontsize=10, fontweight='bold', va='top',
                     bbox=dict(boxstyle='round', facecolor='white', alpha=0.9))

        # 设置坐标轴边界（留边距）
        xs = [x for x, y in pos.values()]
        ys = [y for x, y in pos.values()]
        margin = 10
        self.ax.set_xlim(min(xs) - margin if xs else -70, max(xs) + margin if xs else 70)
        self.ax.set_ylim(min(ys) - margin if ys else -70, max(ys) + margin if ys else 70)
        self.ax.axis('off')  # 隐藏坐标轴

        # 调整布局并渲染
        self.fig.tight_layout(pad=0.3)
        self.canvas.draw()

    def _resolve_batch_file(self, filename):
        """
        在当前工程路径下搜索批量业务文件。
        优先级：
          1) 绝对路径
          2) BASE_DIR/filename
          3) BASE_DIR 递归搜索同名文件
        """
        if not filename:
            raise ValueError("文件名不能为空")

        if os.path.isabs(filename):
            if os.path.exists(filename):
                return filename
            raise FileNotFoundError(f"未找到文件: {filename}")

        direct_path = os.path.join(BASE_DIR, filename)
        if os.path.exists(direct_path):
            return direct_path

        base_name = os.path.basename(filename)
        matches = []
        for root, _, files in os.walk(BASE_DIR):
            if base_name in files:
                matches.append(os.path.join(root, base_name))

        if not matches:
            raise FileNotFoundError(f"在当前路径未找到文件: {filename}")

        matches.sort()
        return matches[0]

    def _parse_batch_requests(self, file_path):
        """
        解析批量业务文件。
        每行格式：src dst qos
        """
        requests = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 3:
                    requests.append(
                        {"line_no": line_no, "error": f"格式错误（需要 src dst qos）: {line}"}
                    )
                    continue
                requests.append(
                    {
                        "line_no": line_no,
                        "src": parts[0],
                        "dst": parts[1],
                        "qos": parts[2],
                    }
                )
        return requests

    def _store_flow_record(self, src_show, dst_show, qos, optimal_path, alternative_paths):
        """
        保存业务流到前端内存并分配颜色。
        """
        fid = f"flow{self.flow_seq:03d}"
        self.flow_seq += 1
        self.flows[fid] = {
            "src": src_show,
            "dst": dst_show,
            "qos": qos,
            "path": optimal_path,
            "alternative_paths": alternative_paths,
        }
        self.flow_colors[fid] = FLOW_COLORS[len(self.flow_colors) % len(FLOW_COLORS)]
        return fid

    def batch_deploy_flows(self):
        """
        批量读取业务文件并逐条部署。
        终端精简显示：
          - 源/目的/QoS
          - 部署路径
          - 单条路由生成和切换耗时
          - 最终平均耗时
        """
        win = tk.Toplevel(self)
        win.title("批量部署业务流")
        win.geometry("560x260")

        tk.Label(
            win,
            text="业务文件名（在当前路径搜索，如 test_traffic.txt）:",
            font=("Microsoft YaHei", 11),
        ).pack(anchor="w", padx=10, pady=5)
        efile = tk.Entry(win, font=("Consolas", 10))
        efile.pack(fill=tk.X, padx=10)
        efile.insert(0, "test_traffic.txt")

        result_var = tk.StringVar(value="等待输入...")
        tk.Label(win, textvariable=result_var, font=("Microsoft YaHei", 10), fg="#1e8449").pack(
            anchor="w", padx=10, pady=8
        )

        def run_batch():
            try:
                raw_name = efile.get().strip()
                file_path = self._resolve_batch_file(raw_name)
                requests = self._parse_batch_requests(file_path)
                if not requests:
                    raise ValueError("文件中没有可执行的业务流")

                self._log_terminal(f"\n>>> [BATCH DEPLOY] file={file_path}\n")
                total = 0
                success = 0
                fail = 0
                route_times = []
                switch_times = []

                for item in requests:
                    total += 1
                    if "error" in item:
                        fail += 1
                        self._log_terminal(f"[Line {item['line_no']}] [FAIL] {item['error']}\n")
                        continue

                    line_no = item["line_no"]
                    src_raw = item["src"]
                    dst_raw = item["dst"]
                    qos_raw = item["qos"]
                    try:
                        qos = float(qos_raw)
                        if qos <= 0:
                            raise ValueError("QoS必须大于0")

                        src = self._normalize_endpoint(src_raw)
                        dst = self._normalize_endpoint(dst_raw)
                        src_show = src.replace("docker", "domain").replace(":", "-")
                        dst_show = dst.replace("docker", "domain").replace(":", "-")

                        t0 = time.time()
                        route_stdout = call_backend_script("route_cal.py", src, dst, qos)
                        route_cost = extract_exec_time(route_stdout)
                        if route_cost is None:
                            route_cost = round(time.time() - t0, 4)

                        route_result = load_route_candidates(src, dst, qos)
                        optimal_path_data = route_result[0]
                        optimal_path = parse_backend_path(
                            optimal_path_data, self.inter_domain_edges
                        )
                        if not optimal_path:
                            raise Exception("解析路径失败")
                        alternative_paths = route_result[1:] if len(route_result) > 1 else []

                        t1 = time.time()
                        deploy_stdout = call_backend_script(
                            "route_path.py", src, dst, qos, extra_args=["--index", "0"]
                        )
                        deploy_cost = extract_exec_time(deploy_stdout)
                        if deploy_cost is None:
                            deploy_cost = round(time.time() - t1, 4)

                        fid = self._store_flow_record(
                            src_show, dst_show, qos, optimal_path, alternative_paths
                        )
                        path_display = " -> ".join(
                            p.replace("docker", "domain") for p in optimal_path
                        )

                        self._log_terminal(
                            f"[{fid}] {src_show} -> {dst_show} | QoS:{qos:g}\n"
                        )
                        self._log_terminal(f"    Path: {path_display}\n")
                        self._log_terminal(
                            f"    route_gen={route_cost:.4f}s | route_switch={deploy_cost:.4f}s\n"
                        )

                        success += 1
                        route_times.append(route_cost)
                        switch_times.append(deploy_cost)
                        result_var.set(
                            f"处理中... {success + fail}/{len(requests)} (成功 {success}, 失败 {fail})"
                        )
                        win.update()

                    except Exception as e:
                        fail += 1
                        self._log_terminal(
                            f"[Line {line_no}] [FAIL] {src_raw} -> {dst_raw} | QoS:{qos_raw} | {str(e)}\n"
                        )
                        result_var.set(
                            f"处理中... {success + fail}/{len(requests)} (成功 {success}, 失败 {fail})"
                        )
                        win.update()

                if route_times and switch_times:
                    avg_route = sum(route_times) / len(route_times)
                    avg_switch = sum(switch_times) / len(switch_times)
                    self.route_gen_time.set(f"{avg_route:.4f}")
                    self.route_switch_time.set(f"{avg_switch:.4f}")
                    self._log_terminal(
                        f"\n[BATCH AVG] route_gen={avg_route:.4f}s | route_switch={avg_switch:.4f}s\n"
                    )
                else:
                    self.route_gen_time.set("0.0000")
                    self.route_switch_time.set("0.0000")

                self._log_terminal(
                    f"[BATCH RESULT] total={total}, success={success}, fail={fail}\n"
                )
                result_var.set(f"完成：总计 {total}，成功 {success}，失败 {fail}")
                self.draw()

            except Exception as e:
                result_var.set(f"错误: {str(e)}")
                self._log_terminal(f"[batch-deploy] failed: {str(e)}\n")

        tk.Button(
            win,
            text="读取并批量部署",
            command=run_batch,
            bg="#1e8449",
            fg="white",
            font=("Microsoft YaHei", 11, "bold"),
        ).pack(fill=tk.X, padx=40, pady=12)

    def add_flow(self):
        """
        弹出对话框添加新业务流（不需要业务流ID）。
        """
        win = tk.Toplevel(self)
        win.title("新增业务流")
        win.geometry("520x320")

        # 源节点输入（支持domainX-hX格式，自动转换为dockerX-hX）
        tk.Label(win, text="源节点 (格式: domain1-h1 / docker1:h1):", font=('Microsoft YaHei', 11)).pack(anchor='w', padx=10, pady=3)
        esrc = tk.Entry(win, font=('Consolas', 10))
        esrc.pack(fill=tk.X, padx=10)
        esrc.insert(0, "domain1-h1")

        # 目的节点输入
        tk.Label(win, text="目的节点 (格式: domain5-h1 / docker5:h1):", font=('Microsoft YaHei', 11)).pack(anchor='w', padx=10, pady=3)
        edst = tk.Entry(win, font=('Consolas', 10))
        edst.pack(fill=tk.X, padx=10)
        edst.insert(0, "domain5-h1")

        # QoS值输入
        tk.Label(win, text="QoS值 (整数):", font=('Microsoft YaHei', 11)).pack(anchor='w', padx=10, pady=3)
        eqos = tk.Entry(win, font=('Consolas', 10))
        eqos.pack(fill=tk.X, padx=10)
        eqos.insert(0, "20")  # 默认值

        # 结果显示标签
        result_var = tk.StringVar(value="等待输入...")
        tk.Label(win, textvariable=result_var, font=('Microsoft YaHei', 10), fg='#28a745').pack(pady=10)

        def calc():
            """执行路径计算（调用后端脚本）"""
            try:
                src_ui = esrc.get().strip()
                dst_ui = edst.get().strip()
                qos = int(eqos.get().strip())
                if qos < 0:
                    raise Exception("QoS值不能为负数")

                src = self._normalize_endpoint(src_ui)
                dst = self._normalize_endpoint(dst_ui)
                src_show = src.replace("docker", "domain").replace(":", "-")
                dst_show = dst.replace("docker", "domain").replace(":", "-")

                self._log_terminal(f"\n>>> [ADD FLOW] {src_show} -> {dst_show} | QoS={qos}\n")

                result_var.set("正在调用route_cal.py计算路由...")
                win.update()  # 刷新界面显示状态
                self._log_terminal(f"$ {self._display_python_cmd('route_cal.py', [src, dst, qos])}\n")
                t0 = time.time()
                route_stdout = call_backend_script("route_cal.py", src, dst, qos)
                route_cost = extract_exec_time(route_stdout)
                if route_cost is None:
                    route_cost = round(time.time() - t0, 4)
                self.route_gen_time.set(f"{route_cost:.4f}")

                # route_cal.py 的结果在 topo_path 文件中
                route_result = load_route_candidates(src, dst, qos)
                self._log_terminal("Candidates (Top 5):\n")
                for line in self._candidate_path_lines(route_result, max_count=5):
                    self._log_terminal(line + "\n")

                # 再调用路径部署脚本
                result_var.set("正在调用route_path.py部署路径...")
                win.update()
                self._log_terminal(f"$ {self._display_python_cmd('route_path.py', [src, dst, qos, '--index', 0])}\n")
                t1 = time.time()
                deploy_stdout = call_backend_script(
                    "route_path.py", src, dst, qos, extra_args=["--index", "0"]
                )
                deploy_cost = extract_exec_time(deploy_stdout)
                if deploy_cost is None:
                    deploy_cost = round(time.time() - t1, 4)
                self.route_switch_time.set(f"{deploy_cost:.4f}")

                optimal_path_data = route_result[0]  # rank=0的最优路径

                optimal_path = parse_backend_path(
                    optimal_path_data, self.inter_domain_edges
                )
                if not optimal_path:
                    raise Exception("解析路径失败")

                alternative_paths = []
                if isinstance(route_result, list) and len(route_result) > 1:
                    alternative_paths = route_result[1:]  # 前1个是最优，剩余是备选

                fid = f"flow{self.flow_seq:03d}"
                self.flow_seq += 1
                self.flows[fid] = {
                    'src': src_show,
                    'dst': dst_show,
                    'qos': qos,
                    'path': optimal_path,  # 最优路径（可视化用）
                    'alternative_paths': alternative_paths  # 备选路径（仅展示）
                }
                self.flow_colors[fid] = FLOW_COLORS[len(self.flow_colors) % len(FLOW_COLORS)]

                result_var.set(
                    f"成功: {fid} | 跳数:{len(optimal_path)-1}, 备选:{len(alternative_paths)}, "
                    f"算路:{route_cost:.4f}s, 部署:{deploy_cost:.4f}s")
                path_display = [p.replace('docker', 'domain') for p in optimal_path]
                score = float(optimal_path_data.get("score", 0))
                self._log_terminal(f"Deploy Route: [0] Score={score:.2f}\n")
                self._log_terminal(f"[{fid}] PATH: {' -> '.join(path_display)}\n")
                bw_lines = self._deploy_bw_lines(deploy_stdout)
                if bw_lines:
                    self._log_terminal("Bandwidth Update:\n")
                    for line in bw_lines:
                        self._log_terminal(line + "\n")
                self._log_terminal(
                    f"[{fid}] DEPLOYED | route={route_cost:.4f}s deploy={deploy_cost:.4f}s alt={len(alternative_paths)}\n"
                )
                self.draw()  # 重绘拓扑显示新路径

            except ValueError as e:
                if "invalid literal" in str(e):
                    result_var.set("错误: QoS值必须为整数")
                else:
                    result_var.set(f"错误: {str(e)}")
            except Exception as e:
                result_var.set(f"错误: {str(e)}")
                self._log_terminal(f"[add-flow] failed: {str(e)}\n")

        tk.Button(win, text="计算并添加路径", command=calc, bg='#28a745', fg='white',
                  font=('Microsoft YaHei', 11, 'bold')).pack(pady=10, fill=tk.X, padx=50)

    def del_link(self):
        """
        链路/节点失效注入对话框。
        支持四类故障：
          1) 域间链路失效/恢复
          2) Docker 节点失效/恢复
          3) 域内链路失效/恢复
          4) 域内交换机失效/恢复
        """
        win = tk.Toplevel(self)
        win.title("链路节点失效")
        win.geometry("680x500")

        type_options = {
            "域间链路": "inter_link",
            "Docker节点": "docker_node",
            "域内链路": "intra_link",
            "域内节点(交换机)": "intra_node",
        }
        action_options = {
            "inter_link": [("失效", "down"), ("恢复", "up")],
            "docker_node": [("失效", "down"), ("恢复", "up")],
            "intra_link": [("失效", "down"), ("恢复", "up")],
            "intra_node": [("失效", "down"), ("恢复", "up")],
        }
        hint_map = {
            "inter_link": "输入格式: docker1:eth3 (每行一个，可多行)",
            "docker_node": "输入格式: docker4 (每行一个，可多行)",
            "intra_link": "输入格式: docker2:s1-eth5 (每行一个，可多行)",
            "intra_node": "输入格式: docker2:s4 (每行一个，可多行)",
        }
        default_text = {
            "inter_link": "docker1:eth3",
            "docker_node": "docker4",
            "intra_link": "docker2:s1-eth5",
            "intra_node": "docker2:s4",
        }

        top = tk.Frame(win)
        top.pack(fill=tk.X, padx=10, pady=8)

        tk.Label(top, text="故障类型:", font=('Microsoft YaHei', 11)).grid(row=0, column=0, sticky='w')
        type_var = tk.StringVar(value="域间链路")
        type_box = ttk.Combobox(
            top, textvariable=type_var, state="readonly", values=list(type_options.keys()), width=18
        )
        type_box.grid(row=0, column=1, sticky='w', padx=6)

        tk.Label(top, text="操作:", font=('Microsoft YaHei', 11)).grid(row=0, column=2, sticky='w', padx=(20, 0))
        action_var = tk.StringVar()
        action_box = ttk.Combobox(top, textvariable=action_var, state="readonly", width=12)
        action_box.grid(row=0, column=3, sticky='w', padx=6)

        hint_var = tk.StringVar()
        tk.Label(win, textvariable=hint_var, font=('Microsoft YaHei', 10), fg='#34495e').pack(anchor='w', padx=10)

        input_text = tk.Text(win, height=12, font=('Consolas', 10))
        input_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        def apply_type_defaults(*_):
            tkey = type_options[type_var.get()]
            choices = [name for name, _ in action_options[tkey]]
            action_box["values"] = choices
            action_var.set(choices[0])
            hint_var.set(hint_map[tkey])
            input_text.delete("1.0", tk.END)
            input_text.insert("1.0", default_text[tkey])

        type_var.trace_add("write", apply_type_defaults)
        apply_type_defaults()

        def parse_targets(raw):
            targets = []
            for line in raw.splitlines():
                for part in line.split(","):
                    val = part.strip()
                    if val:
                        targets.append(val)
            return targets

        def build_cmds(ftype, action, targets):
            cmds = []
            action_val = dict(action_options[ftype]).get(action)
            if action_val is None:
                raise ValueError("操作类型无效")

            for target in targets:
                if ftype in ("inter_link", "intra_link"):
                    if ":" not in target:
                        raise ValueError(f"格式错误: {target}")
                    container, iface = target.split(":", 1)
                    container, iface = container.strip(), iface.strip()
                    if not container.startswith("docker") or not iface:
                        raise ValueError(f"格式错误: {target}")
                    base = ["docker", "exec", container, "ip", "link", "set", iface, action_val]
                    cmds.append((target, self._build_cmd_with_sudo(base)))

                elif ftype == "docker_node":
                    container = target.strip()
                    if not container.startswith("docker"):
                        raise ValueError(f"格式错误: {target}")
                    op = "stop" if action_val == "down" else "start"
                    base = ["docker", op, container]
                    cmds.append((target, self._build_cmd_with_sudo(base)))

                elif ftype == "intra_node":
                    if ":" not in target:
                        raise ValueError(f"格式错误: {target}")
                    container, sw = target.split(":", 1)
                    container, sw = container.strip(), sw.strip()
                    if not container.startswith("docker") or not sw.startswith("s"):
                        raise ValueError(f"格式错误: {target}")
                    if action_val == "down":
                        base = ["docker", "exec", container, "ovs-vsctl", "--if-exists", "del-br", sw]
                        cmds.append((target, self._build_cmd_with_sudo(base)))
                    else:
                        add_cmd = ["docker", "exec", container, "ovs-vsctl", "--may-exist", "add-br", sw]
                        up_cmd = ["docker", "exec", container, "ip", "link", "set", sw, "up"]
                        cmds.append((f"{target} [add-br]", self._build_cmd_with_sudo(add_cmd)))
                        cmds.append((f"{target} [link-up]", self._build_cmd_with_sudo(up_cmd)))

            return cmds

        def execute():
            ftype = type_options[type_var.get()]
            action = action_var.get().strip()
            targets = parse_targets(input_text.get("1.0", tk.END))
            if not targets:
                messagebox.showwarning("提示", "请输入目标。")
                return
            try:
                cmds = build_cmds(ftype, action, targets)
            except Exception as e:
                messagebox.showerror("输入错误", str(e))
                return

            self._log_terminal(
                f"\n>>> [FAULT] type={type_var.get()} action={action} targets={len(targets)}\n"
            )
            success = 0
            for desc, cmd in cmds:
                self._log_terminal(f"$ {self._format_cmd_for_log(cmd)}\n")
                ok, output = self._run_cmd_capture(cmd, timeout=60)
                if output:
                    self._log_terminal(output if output.endswith("\n") else output + "\n")
                if ok:
                    success += 1
                    self._log_terminal(f"[OK] {desc}\n")
                else:
                    self._log_terminal(f"[FAIL] {desc}\n")

            if ftype == "intra_node" and dict(action_options[ftype]).get(action) == "up":
                self._log_terminal(
                    "[notice] 域内交换机恢复仅做 add-br/link-up，完整端口连接建议执行“网络生成”。\n"
                )

            self._log_terminal(f"[FAULT] done: success={success}/{len(cmds)}\n")
            self._reload_topology_data(silent=True)
            self.draw()
            win.destroy()
            messagebox.showinfo("完成", f"执行完成: {success}/{len(cmds)} 条命令成功")

        tk.Button(
            win, text="执行", command=execute,
            bg='#dc3545', fg='white', font=('Microsoft YaHei', 12, 'bold')
        ).pack(fill=tk.X, padx=120, pady=10)

    def clear_terminal(self):
        """清空终端显示区域。"""
        self.terminal_txt.delete(1.0, tk.END)
        self.terminal_txt.insert(1.0, "已清空\n")

    def export_terminal(self):
        """导出终端内容到文本文件。"""
        content = self.terminal_txt.get(1.0, tk.END).strip()
        if not content:
            messagebox.showwarning("提示", "终端内容为空")
            return

        f = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile=f"terminal_{int(time.time())}.txt"
        )

        if f:
            with open(f, 'w', encoding='utf-8') as file:
                file.write(self.terminal_txt.get(1.0, tk.END))
            messagebox.showinfo("成功", f"已导出到:\n{f}")


# ===================== 程序入口 =====================
if __name__ == "__main__":
    # 设置Matplotlib中文字体支持（适配Linux系统常见中文字体）
    plt.rcParams['font.sans-serif'] = ['WenQuanYi Zen Hei', 'DejaVu Sans', 'SimHei']
    plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示问题

    # 创建并运行GUI应用
    app = SDN_GUI()
    app.mainloop()
