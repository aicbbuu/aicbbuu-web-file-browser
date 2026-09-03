#!/usr/bin/env bash
# ============================================================
#  aicbbuu网页文件浏览 —— 便携版安装脚本
#  用法: sudo bash install.sh
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)"
SERVICE_NAME="web-file-browser-server"

echo "=============================================="
echo "  aicbbuu网页文件浏览 便携版安装"
echo "=============================================="

# 0. 检查 root
if [[ $EUID -ne 0 ]]; then
    echo "❌ 请以 root 运行: sudo bash install.sh"
    exit 1
fi

# 1. 检查 python3
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ 未找到 python3，本服务需要 Python 3.8+"
    exit 1
fi
PYVER=$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "✓ 检测到 Python $PYVER"

# 2. 交互配置
read -rp "资源根目录路径 [/srv/resource]: " RESOURCE_DIR
RESOURCE_DIR="${RESOURCE_DIR:-/srv/resource}"
read -rp "监听端口 [60010]: " RESOURCE_PORT
RESOURCE_PORT="${RESOURCE_PORT:-60010}"
# 交互配置（密码必填，无默认值以保证脱敏）
read -rp "登录密码（必填，请设置强密码）: " RESOURCE_PASSWORD
if [[ -z "$RESOURCE_PASSWORD" ]]; then
    echo "❌ 密码不能为空，为保证安全，请设置一个密码。"
    exit 1
fi
echo "提示: 服务器将以上述端口/密码启动，登录账号为 admin。"

# 3. 安装脚本
echo "--- 复制主脚本到 /usr/local/bin/ ---"
install -m 755 "${SCRIPT_DIR}/web_file_browser.py" /usr/local/bin/web_file_browser.py

# 4. 创建资源目录
mkdir -p "$RESOURCE_DIR"
chmod 755 "$RESOURCE_DIR"
cat > "$RESOURCE_DIR/index.txt" <<EOF
欢迎使用 aicbbuu网页文件浏览
====================
这是你的文件根目录，把文件放进来即可通过 Web 访问。
EOF

# 5. 生成 systemd 服务
echo "--- 创建 systemd 服务 ---"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=aicbbuu网页文件浏览
After=network.target

[Service]
Type=simple
WorkingDirectory=$RESOURCE_DIR
Environment=RESOURCE_DIR=$RESOURCE_DIR
Environment=RESOURCE_PORT=$RESOURCE_PORT
Environment=RESOURCE_USER=admin
Environment=RESOURCE_PASSWORD=${RESOURCE_PASSWORD}
ExecStart=/usr/bin/python3 -B /usr/local/bin/web_file_browser.py
Restart=always
RestartSec=3
StartLimitInterval=60
StartLimitBurst=10
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$RESOURCE_DIR
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

# 6. 启用并启动
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "=============================================="
    echo " ✅ 安装成功！"
    echo "    地址: http://<本机IP>:${RESOURCE_PORT}"
    echo "    用户名: admin"
    echo "    资源目录: ${RESOURCE_DIR}"
    echo "    查看状态: systemctl status ${SERVICE_NAME}"
    echo "    实时日志: journalctl -u ${SERVICE_NAME} -f"
    echo "=============================================="
else
    echo "❌ 服务启动失败，检查日志: journalctl -u ${SERVICE_NAME}"
    exit 1
fi