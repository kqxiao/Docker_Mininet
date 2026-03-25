# docker_mininet 项目说明

## 1. 文件分类与作用

### 1.1 拓扑构建与部署
- `autobuild_out.py`：构建域间网络（Docker + 宿主机 OVS `br-sdn`），生成 `inter_domain_db.json`；支持随机预设与固定场景域间预设。
- `autobuild_in_agent.py`：在每个 Docker 容器内构建域内 Mininet 拓扑（交换机/主机/链路），并导出 `topo_db.json`；支持随机预设与固定场景域内预设。
- `master_deploy.py`：一键编排部署。先跑域间，再启动各容器域内拓扑，最后回收到 `topo_data/`；支持 `--scenario` 一键选择固定场景。

### 1.2 AI 算路模型
- `gnn_lyx.py`：GNN 模型结构定义（注意力 + GRU + 读出层）。
- `myClass.py`：图结构适配和候选路径特征构造（供 GNN 推理）。
- `model_epoch_20000.pth`：训练好的模型参数。

### 1.3 路由计算与下发
- `route_cal.py`：按源/宿主机和带宽需求算路，输出候选路径到 `topo_path/*.json`。
- `route_path.py`：读取候选路径并下发流表，同时更新剩余带宽；维护 `topo_data/active_flows.json` 活动业务状态。
- `route_batch.py`：批量读取业务请求文件并依次执行算路+下发。

### 1.4 故障监测与自愈
- `topo_monitor.py`：持续探测故障并自动重路由重部署。
- 域间故障：容器间 `ethX` 端口 down、容器节点 down。
- 域内故障：交换机端口 down、交换机 down（被删除/不可用）。

### 1.5 UI 与工具
- `ui.py`：图形界面入口（调用后端脚本算路和部署），支持从 `topo_data/` 读取拓扑并按实际域数量动态显示图例。
- `ovs-docker`：连接 Docker 容器网卡到 OVS 网桥的工具脚本。

### 1.6 镜像与启动脚本
- `Dockerfile`：构建运行镜像 `mininet_docker`。
- `ENTRYPOINT.sh`：容器启动脚本。
- `m` 脚本（需提前放到宿主机 PATH）：用于定位 Mininet 主机命名空间，支持 `docker exec <container> m <host> ...`。

### 1.7 运行时数据目录
- `topo_data/`：拓扑数据库与剩余带宽状态（含 `active_flows.json`）。
- `topo_path/`：算路生成的候选路径文件。
- `test_traffic.txt`：批量业务请求样例文件。

## 2. 基础运行命令

### 2.1 构建镜像
```bash
sudo docker build -t mininet_docker .
```

### 2.2 一键部署仿真环境
```bash
sudo python3 master_deploy.py
```

### 2.3 查看可选拓扑预设
```bash
# 域间拓扑预设
python3 master_deploy.py --list-inter-presets

# 域内拓扑预设
python3 master_deploy.py --list-intra-presets

# 固定场景映射（inter/intra 预设组合）
python3 master_deploy.py --list-scenarios
```

### 2.4 选择不同拓扑进行建设（示例）
#### 2.4.1 随机拓扑预设
可用预设：

- 域间：`ba_core` / `ring5` / `star5` / `mesh5`
- 域内：`ba_core` / `ring20` / `ws20` / `er20` / `tree20`

预设含义说明：

- `域间 ba_core`：BA 无标度网络，参数 `BA(5,2)`，会形成少数高连接“核心域”。
- `域间 ring5`：5 节点环形拓扑，每个域与前后两个域相连。
- `域间 star5`：5 节点星型拓扑，1 个中心域连接其余 4 个边缘域。
- `域间 mesh5`：5 节点全互联拓扑，任意两个域都直连。

- `域内 ba_core`：BA 无标度网络，参数 `BA(20,2)`，存在枢纽交换机。
- `域内 ring20`：20 交换机环形拓扑，结构规则，路径冗余较少。
- `域内 ws20`：WS 小世界网络，参数 `WS(20,4,0.25)`，兼顾局部聚类与短路径。
- `域内 er20`：ER 随机图，参数 `ER(20,0.18)`，并保证图连通。
- `域内 tree20`：20 节点随机树，边数固定为 `n-1=19`，无环、路径唯一。

```bash
# 示例1：域间环形 + 域内小世界
sudo python3 master_deploy.py --inter-preset ring5 --intra-preset ws20

# 示例2：域间全互联 + 域内随机图（连通）
sudo python3 master_deploy.py --inter-preset mesh5 --intra-preset er20
```

#### 2.4.2 固定场景拓扑预设
固定场景通过 `--scenario` 直接选择（会自动覆盖 `--inter-preset/--intra-preset`）：

- `infantry`：步兵场景（6 域固定域间 + 步兵域内固定拓扑）
- `recon`：侦查场景（6 域固定域间 + 侦查域内固定拓扑）
- `fire_support`：火力支援场景（6 域固定域间 + 火力支援域内固定拓扑）

```bash
sudo python3 master_deploy.py --scenario infantry
sudo python3 master_deploy.py --scenario recon
sudo python3 master_deploy.py --scenario fire_support
```


#### 2.4.3 域内拓扑建立优化说明（减少等待时间）

为降低“域内拓扑长时间未就绪”的概率，当前版本做了以下优化：

1. `topo_agent` 默认静默启动（`--quiet`）
- 位置：`autobuild_in_agent.py`
- 机制：减少容器内大量 `info` 打印与 Mininet 启动日志输出，降低 I/O 开销和启动抖动。
- 效果：多容器并发拉起时更稳定，平均就绪时间更短。

2. 启动错峰（`--intra-start-stagger`）
- 位置：`master_deploy.py`
- 机制：各容器不是完全同一时刻启动域内构建，而是按小间隔依次启动（默认 0.2 秒）。
- 效果：降低同一时刻 CPU/内核网络资源争抢，减少个别容器“卡住”概率。

3. 自动重试拉起（`--intra-restart-after` + `--intra-max-retries`）
- 位置：`master_deploy.py`
- 机制：若某个容器在设定时间内仍未生成 `/root/topo_db.json`，自动重启该容器内 `topo_agent`。
- 默认值：
  - `--intra-restart-after 45`（单容器 45 秒未就绪触发重启）
  - `--intra-max-retries 1`（每个容器最多自动重试 1 次）
- 效果：避免“个别容器卡死导致整体等待到总超时”的情况。

4. 更直观的等待信息
- 位置：`master_deploy.py`
- 机制：等待输出改为 `dockerX(等待秒数,重试次数)` 格式，例如 `docker3(47s,r1)`。
- 效果：可直接看到是“慢”还是“卡”，便于调参。

常用命令（已启用优化默认参数）：
```bash
sudo python3 master_deploy.py --inter-preset ring5 --intra-preset ring20
```

需要更激进减少等待时（可选）：
```bash
sudo python3 master_deploy.py --inter-preset ring5 --intra-preset ring20 \
  --intra-restart-after 30 \
  --intra-max-retries 2 \
  --intra-start-stagger 0.15
```

如果要排查容器内启动细节（打开详细日志）：
```bash
sudo python3 master_deploy.py --inter-preset ring5 --intra-preset ring20 --intra-verbose
```


### 2.5 单条业务算路与下发（示例）
```bash
sudo python3 route_cal.py docker2:h1 docker5:h1 20
sudo python3 route_path.py docker2:h1 docker5:h1 20 --index 0
```


### 2.6 批量业务（可选）
```bash
sudo python3 route_batch.py test_traffic.txt
```

## 3. 故障自愈联调命令（完整）

### 3.1 启动监控
```bash
sudo python3 topo_monitor.py --interval 5
```

### 3.2 域间故障注入
```bash
# 1) 关闭域间链路端口（示例：docker1:eth3）
sudo docker exec docker1 ip link set eth3 down

# 2) 恢复域间链路端口
sudo docker exec docker1 ip link set eth3 up

# 3) 节点故障（停止容器）
sudo docker stop docker4
```

### 3.3 域内故障注入
```bash
# 1) 域内交换机端口故障（示例：docker2:s1-eth5）
sudo docker exec docker2 ip link set s1-eth5 down

# 2) 域内交换机故障（示例：docker2:s4）
sudo docker exec docker2 ovs-vsctl del-br s4
```

### 3.4 状态与连通性验证
```bash
# 查看当前活动业务及其重路由结果
cat topo_data/active_flows.json

# 查看容器运行状态
sudo docker ps --format '{{.Names}}'

# 连通性测试（示例：docker2:h1 -> docker5:h1）
sudo docker exec docker2 m h1 ping -c 3 -W 1 10.0.5.1


### 3.5 结束与恢复环境
```bash
# 结束监控
# 在 topo_monitor.py 运行终端按 Ctrl+C

# 若做过交换机删除/多次故障注入，建议重建环境
sudo python3 master_deploy.py
```
