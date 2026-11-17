#!/usr/bin/env bash
# ============================================================================
#  aicbbuu网页文件浏览 —— 卸载脚本
#
#  用法： sudo bash uninstall.sh
#
#  会停掉并删除服务与程序，但【不会】删除：
#    - 你共享的文件目录
#    - 配置文件（你可以自己留着，或手动删掉）
# ============================================================================
set -euo pipefail

APP_NAME="aicbbuu网页文件浏览"
SERVICE_NAME="web-file-browser"
INSTALL_DIR="/opt/web-file-browser"

info() { printf '\033[36m%s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash uninstall.sh"

echo "=============================================="
echo "  ${APP_NAME} 卸载"
echo "=============================================="
echo
warn "将要停止并移除服务与程序目录 ${INSTALL_DIR}"
warn "你的共享文件目录和配置文件不会被删除。"
echo
read -rp "确认卸载？[y/N]: " CONFIRM
[[ "${CONFIRM:-N}" =~ ^[Yy]$ ]] || { echo "已取消"; exit 0; }

# 先读配置，好在最后告诉用户共享目录在哪
RUN_USER="${SUDO_USER:-}"
RESOURCE_DIR=""
ENV_FILE=""
if [[ -n "$RUN_USER" && "$RUN_USER" != "root" ]]; then
    RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
    ENV_FILE="${RUN_HOME}/.config/web-file-browser.env"
    if [[ -f "$ENV_FILE" ]]; then
        # shellcheck disable=SC1090
        RESOURCE_DIR="$(grep -E '^RESOURCE_DIR=' "$ENV_FILE" | cut -d= -f2- || true)"
    fi
fi

info "---- 停止服务 ----"
if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload
    ok "服务已移除"
else
    warn "没有找到已安装的服务"
fi

info "---- 删除程序目录 ----"
rm -rf "$INSTALL_DIR"
ok "已删除 ${INSTALL_DIR}"

echo
echo "=============================================="
echo "  卸载完成"
echo "=============================================="
echo
[[ -n "$RESOURCE_DIR" ]] && echo "  你的文件还在：${RESOURCE_DIR}"
echo "  配置文件保留在：${ENV_FILE:-（未找到）}"
echo
echo "  想彻底清干净，手动删这两处即可。"
echo
