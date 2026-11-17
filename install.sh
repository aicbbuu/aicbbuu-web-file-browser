#!/usr/bin/env bash
# ============================================================================
#  aicbbuu网页文件浏览 —— 安装脚本
#
#  用法： sudo bash install.sh
#
#  会在你的机器上：
#    1. 把程序复制到 /opt/web-file-browser
#    2. 生成自签证书（可选）
#    3. 写一个 systemd 服务，开机自启、崩溃自愈
#    4. 用你自己的普通用户身份运行（不是 root）
#
#  卸载： sudo bash uninstall.sh
# ============================================================================
set -euo pipefail

APP_NAME="aicbbuu网页文件浏览"
SERVICE_NAME="web-file-browser"
INSTALL_DIR="/opt/web-file-browser"

info()  { printf '\033[36m%s\033[0m\n' "$*"; }
ok()    { printf '\033[32m✓ %s\033[0m\n' "$*"; }
warn()  { printf '\033[33m! %s\033[0m\n' "$*"; }
die()   { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

echo "=============================================="
echo "  ${APP_NAME} 安装"
echo "=============================================="
echo

# ---------- 1. 前置检查 ----------
[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash install.sh"

# 服务必须以普通用户身份跑，所以必须是通过 sudo 启动的
RUN_USER="${SUDO_USER:-}"
if [[ -z "$RUN_USER" || "$RUN_USER" == "root" ]]; then
    die "请用 sudo 从普通用户账户运行（不要直接 su 到 root）：
    sudo bash install.sh"
fi
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
[[ -n "$RUN_HOME" ]] || die "找不到用户 $RUN_USER 的家目录"
ok "服务将以用户 ${RUN_USER} 运行"

command -v python3 >/dev/null 2>&1 || die "未找到 python3，本程序需要 Python 3.8+"
PYOK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,8) else 0)')
[[ "$PYOK" == "1" ]] || die "Python 版本过低（需要 3.8+），当前：$(python3 -V)"
ok "Python $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"

[[ -f "$(dirname "${BASH_SOURCE[0]}")/web_file_browser.py" ]] \
    || die "请在本项目目录里运行此脚本"

# ---------- 2. 交互配置 ----------
echo
info "---- 配置 ----"

read -rp "要共享的目录 [${RUN_HOME}/resource]: " RESOURCE_DIR
RESOURCE_DIR="${RESOURCE_DIR:-${RUN_HOME}/resource}"

read -rp "监听端口 [443]: " RESOURCE_PORT
RESOURCE_PORT="${RESOURCE_PORT:-443}"

read -rp "登录账号 [admin]: " RESOURCE_USER
RESOURCE_USER="${RESOURCE_USER:-admin}"

while true; do
    read -rsp "登录密码（必填，输入时不显示）: " RESOURCE_PASSWORD
    echo
    [[ -n "$RESOURCE_PASSWORD" ]] && break
    warn "密码不能为空 —— 本程序不内置任何默认口令"
done

read -rp "是否生成自签 HTTPS 证书？[Y/n]: " GEN_CERT
GEN_CERT="${GEN_CERT:-Y}"

echo
info "---- 即将安装 ----"
echo "  程序目录    ${INSTALL_DIR}"
echo "  共享目录    ${RESOURCE_DIR}"
echo "  监听端口    ${RESOURCE_PORT}"
echo "  登录账号    ${RESOURCE_USER}"
echo "  运行用户    ${RUN_USER}"
read -rp "确认继续？[Y/n]: " CONFIRM
[[ "${CONFIRM:-Y}" =~ ^[Yy]?$ ]] || { echo "已取消"; exit 0; }

# ---------- 3. 复制程序 ----------
echo
info "---- 复制程序 ----"
mkdir -p "$INSTALL_DIR"
SCRIPT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

rm -rf "${INSTALL_DIR}/app" "${INSTALL_DIR}/static" "${INSTALL_DIR}/templates"
cp -r "${SCRIPT_SRC}/web_file_browser.py" "${SCRIPT_SRC}/app" \
      "${SCRIPT_SRC}/static" "${SCRIPT_SRC}/templates" "$INSTALL_DIR/"
find "$INSTALL_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
chmod -R a+rX "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR/web_file_browser.py"
ok "已复制到 ${INSTALL_DIR}"

mkdir -p "$RESOURCE_DIR"
chown "$RUN_USER" "$RESOURCE_DIR" 2>/dev/null || true
ok "共享目录就绪：${RESOURCE_DIR}"

mkdir -p "${INSTALL_DIR}/data" "${INSTALL_DIR}/thumbs" "${INSTALL_DIR}/certs"
chown -R "$RUN_USER" "${INSTALL_DIR}/data" "${INSTALL_DIR}/thumbs" "${INSTALL_DIR}/certs"
ok "数据目录就绪"

# ---------- 4. 生成自签证书 ----------
CERT_FILE="${INSTALL_DIR}/certs/server.crt"
KEY_FILE="${INSTALL_DIR}/certs/server.key"
TLS_FLAG=0

if [[ "${GEN_CERT}" =~ ^[Yy] ]]; then
    if command -v openssl >/dev/null 2>&1; then
        info "---- 生成自签证书 ----"
        LOCAL_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
        LOCAL_IP="${LOCAL_IP:-127.0.0.1}"
        HOSTNAME_S="$(hostname 2>/dev/null || echo localhost)"
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
            -keyout "$KEY_FILE" -out "$CERT_FILE" \
            -subj "/CN=${HOSTNAME_S}" \
            -addext "subjectAltName=IP:${LOCAL_IP},IP:127.0.0.1,DNS:${HOSTNAME_S},DNS:localhost" \
            >/dev/null 2>&1 || warn "证书生成失败，将用 HTTP 运行"
        if [[ -f "$CERT_FILE" && -f "$KEY_FILE" ]]; then
            chmod 600 "$KEY_FILE"
            chmod 644 "$CERT_FILE"
            chown "$RUN_USER" "$KEY_FILE" "$CERT_FILE"
            TLS_FLAG=1
            ok "证书已生成（含本机 IP ${LOCAL_IP}）"
        fi
    else
        warn "未安装 openssl，跳过证书生成（将用 HTTP 运行）"
    fi
fi

# ---------- 5. 写环境变量文件 ----------
echo
info "---- 写配置 ----"
ENV_DIR="${RUN_HOME}/.config"
ENV_FILE="${ENV_DIR}/web-file-browser.env"
mkdir -p "$ENV_DIR"
cat > "$ENV_FILE" <<EOF
# ${APP_NAME} 配置
# 改完需要重启服务： sudo systemctl restart ${SERVICE_NAME}

RESOURCE_PASSWORD=${RESOURCE_PASSWORD}
RESOURCE_USER=${RESOURCE_USER}
RESOURCE_DIR=${RESOURCE_DIR}
RESOURCE_PORT=${RESOURCE_PORT}
RESOURCE_HOST=0.0.0.0
RESOURCE_TLS=${TLS_FLAG}
EOF
chown "$RUN_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "配置已写入 ${ENV_FILE}（权限 600）"

# ---------- 6. systemd 服务 ----------
info "---- 安装 systemd 服务 ----"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=${APP_NAME}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=$(id -gn "$RUN_USER")
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/python3 -B ${INSTALL_DIR}/web_file_browser.py
Restart=always
RestartSec=3

# 监听 443 需要绑定特权端口的许可。
# 只放宽这一项，进程本身仍是普通用户 ${RUN_USER} 的权限。
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

# 基础加固
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF
ok "服务单元已写入 /etc/systemd/system/${SERVICE_NAME}.service"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
systemctl restart "$SERVICE_NAME"

# ---------- 7. 检查结果 ----------
echo
info "---- 检查启动结果 ----"
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "服务已启动"
    SCHEME="http"; [[ "$TLS_FLAG" == "1" ]] && SCHEME="https"
    PORTSHOW=""; [[ "$RESOURCE_PORT" != "443" && "$RESOURCE_PORT" != "80" ]] && PORTSHOW=":${RESOURCE_PORT}"
    LOCAL_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
    echo
    echo "=============================================="
    echo "  安装完成"
    echo "=============================================="
    echo
    echo "  在这台机器或同一局域网的其他设备打开："
    echo
    echo "      ${SCHEME}://${LOCAL_IP}${PORTSHOW}/"
    echo
    echo "  账号：${RESOURCE_USER}"
    echo "  密码：你刚才设置的密码"
    echo
    [[ "$TLS_FLAG" == "1" ]] && echo "  （自签证书，浏览器首次会提示「不安全」——点「继续前往」即可）"
    echo
    echo "  常用命令："
    echo "    sudo systemctl status  ${SERVICE_NAME}"
    echo "    sudo systemctl restart ${SERVICE_NAME}"
    echo "    sudo journalctl -u ${SERVICE_NAME} -f"
    echo "    sudo bash uninstall.sh          # 卸载"
    echo
else
    warn "服务未能正常启动，最近日志："
    echo "----------------------------------------------"
    journalctl -u "$SERVICE_NAME" -n 20 --no-pager || true
    echo "----------------------------------------------"
    if [[ "$RESOURCE_PORT" == "443" ]]; then
        echo
        echo "如果提示地址已被占用，说明 443 端口被 nginx/apache 之类占着："
        echo "  sudo ss -tlnp | grep ':443 '"
        echo "换一个端口重新装即可（比如 8443）。"
    fi
    exit 1
fi
