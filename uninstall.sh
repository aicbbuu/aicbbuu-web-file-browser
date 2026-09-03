#!/usr/bin/env bash
# ============================================================
#  aicbbuu网页文件浏览 —— 便携版卸载脚本
#  用法: sudo bash uninstall.sh
# ============================================================
set -e

SERVICE_NAME="web-file-browser-server"

echo "=============================================="
echo "  卸载 aicbbuu网页文件浏览"
echo "=============================================="

if [[ $EUID -ne 0 ]]; then
    echo "❌ 请以 root 运行: sudo bash uninstall.sh"
    exit 1
fi

# 确认卸载
read -rp "确认卸载服务（不删除资源目录）? [y/N]: " CONFIRM
[[ "${CONFIRM,,}" == "y" ]] || { echo "已取消"; exit 0; }

# 停止并禁用服务
if systemctl list-unit-files | grep -q "${SERVICE_NAME}.service"; then
    echo "--- 停止服务 ---"
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    echo "--- 禁用开机自启 ---"
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true
fi

# 删除服务单元
if [[ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]]; then
    echo "--- 删除服务单元 ---"
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload
fi

# 删除脚本
if [[ -f /usr/local/bin/web_file_browser.py ]]; then
    echo "--- 删除主脚本 ---"
    rm -f /usr/local/bin/web_file_browser.py
fi

echo "✅ 卸载完成！"
echo "   注意：资源目录（如 /srv/resource）未删除，已保留数据。"
echo "   如需彻底删除，请手动移除资源目录。"