#!/usr/bin/env bash
# WXEdge 魔方管理器 · 卸载脚本
set -euo pipefail
PREFIX="/opt/wxedge-manager"
DATA_DIR="/var/lib/wxedge-manager"
PURGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge-data) PURGE=1; shift ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 或 sudo 运行" >&2
  exit 1
fi

echo "==> 停止服务"
if command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now wxedge-manager >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/wxedge-manager.service
  systemctl daemon-reload
fi
rm -f /usr/local/bin/wxedge-manager
rm -rf "$PREFIX"
echo "已移除 $PREFIX 和命令 wxedge-manager"

if [[ "$PURGE" -eq 1 ]]; then
  echo "==> 删除数据目录（含实例数据与 wxnode）：$DATA_DIR"
  rm -rf "$DATA_DIR"
else
  echo "数据目录保留：$DATA_DIR（需要彻底删除请加 --purge-data）"
fi
echo "卸载完成。Docker 与已创建的网心云容器不会自动删除，请用 docker ps 自行确认。"
