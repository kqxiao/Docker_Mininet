# 1. 指定基础镜像
FROM ubuntu:22.04

# 2. 设置元数据
LABEL maintainer="User"
LABEL description="Ubuntu 22.04 with Mininet, OVS"

# 3. 设置非交互模式
ENV DEBIAN_FRONTEND=noninteractive

# 4. 设置工作目录
WORKDIR /root

# 5. 安装依赖包
# 综合了基础网络工具、Mininet、OVS 以及 python3-networkx
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    dnsutils \
    ifupdown \
    iproute2 \
    iptables \
    iputils-ping \
    mininet \
    net-tools \
    openvswitch-switch \
    openvswitch-testcontroller \
    python3-networkx \
    tcpdump \
    vim \
    x11-xserver-utils \
    xterm \
    && rm -rf /var/lib/apt/lists/* \
    && touch /etc/network/interfaces

# 6. 复制脚本文件
# 复制 OVS 启动脚本
COPY ENTRYPOINT.sh /ENTRYPOINT.sh
# 复制自定义工具脚本 'm' 到 /bin 目录
COPY m /bin/m

# 7. 赋予脚本执行权限 (合并到一个 RUN 指令中)
RUN chmod +x /ENTRYPOINT.sh /bin/m

# 8. 暴露 OVS 和 OpenFlow 相关端口
EXPOSE 6633 6640 6653

# 9. 设置容器启动时的入口点 (确保 OVS 服务启动)
ENTRYPOINT ["/ENTRYPOINT.sh"]

# 10. 设置默认命令
CMD ["/bin/bash"]
