#!/usr/bin/env bash
# WXEdge 魔方管理器 · Ubuntu 安装脚本
# 用法: sudo ./install.sh [--port 8080] [--host 0.0.0.0] [--data-dir /var/lib/wxedge-manager]
#                          [--skip-docker] [--skip-service]
set -euo pipefail

PREFIX="/opt/wxedge-manager"
DATA_DIR="/var/lib/wxedge-manager"
HOST="0.0.0.0"
PORT="8080"
SKIP_DOCKER=0
SKIP_SERVICE=0

usage() {
  cat <<'EOF'
WXEdge 魔方管理器 安装脚本（Ubuntu / Debian）

选项:
  --prefix PATH       安装目录（默认 /opt/wxedge-manager）
  --data-dir PATH     数据目录（默认 /var/lib/wxedge-manager）
  --host ADDR         监听地址（默认 0.0.0.0）
  --port PORT         监听端口（默认 8080）
  --skip-docker       跳过 Docker 安装（已有 Docker 或仅测试模拟模式时使用）
  --skip-service      不安装/启动 systemd 服务（用于 WSL 等无 systemd 环境）
  -h, --help          显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --skip-docker) SKIP_DOCKER=1; shift ;;
    --skip-service) SKIP_SERVICE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 1 ;;
  esac
done

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -d "$SRC_DIR/app" ]]; then
  echo "错误：在 $SRC_DIR 下找不到 app 目录，请在项目根目录运行本脚本" >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "错误：请使用 root 或 sudo 运行（安装到 $PREFIX 并创建 systemd 服务）" >&2
  exit 1
fi

echo "==> 1/4 检查依赖"
if command -v apt-get >/dev/null 2>&1; then
  if ! command -v python3 >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3
  fi
  if [[ "$SKIP_DOCKER" -eq 0 ]] && ! command -v docker >/dev/null 2>&1; then
    echo "   安装 Docker Engine（docker.io）..."
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
  fi
elif ! command -v python3 >/dev/null 2>&1; then
  echo "错误：未找到 python3，请先安装 Python 3.8+" >&2
  exit 1
fi
if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now docker >/dev/null 2>&1 || true
fi

echo "==> 2/4 安装文件到 $PREFIX"
install -d -m 0755 "$PREFIX" "$PREFIX/app" "$DATA_DIR"
cp -a "$SRC_DIR/app/." "$PREFIX/app/"
install -m 0644 "$SRC_DIR/README.md" "$PREFIX/README.md" 2>/dev/null || true
chmod -R a+rX "$PREFIX/app"

echo "==> 3/4 写入初始面板配置并创建命令 /usr/local/bin/wxedge-manager"
python3 - "$DATA_DIR" "$HOST" "$PORT" "$PREFIX" <<'PY'
import sys
sys.path.insert(0, sys.argv[4] + "/app")
from store import Store
store = Store(sys.argv[1])
store.set_settings({"panel_bind_host": sys.argv[2], "panel_bind_port": int(sys.argv[3])})
print("    面板监听: %s:%s" % (sys.argv[2], sys.argv[3]))
PY
cat > /usr/local/bin/wxedge-manager <<EOF
#!/usr/bin/env bash
export WXEDGE_DATA_DIR="\${WXEDGE_DATA_DIR:-$DATA_DIR}"
ARGS=(--data-dir "\$WXEDGE_DATA_DIR")
if [ -n "\${WXEDGE_HOST:-}" ]; then ARGS+=(--host "\$WXEDGE_HOST"); fi
if [ -n "\${WXEDGE_PORT:-}" ]; then ARGS+=(--port "\$WXEDGE_PORT"); fi
exec /usr/bin/python3 "$PREFIX/app/main.py" "\${ARGS[@]}" "\$@"
EOF
chmod 0755 /usr/local/bin/wxedge-manager

if [[ "$SKIP_SERVICE" -eq 0 ]] && command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  echo "==> 4/4 安装并启动 systemd 服务"
  cat > /etc/systemd/system/wxedge-manager.service <<EOF
[Unit]
Description=WXEdge 魔方管理器 - 网心云容器多开 WebUI
Documentation=file://$PREFIX/README.md
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=simple
Environment=WXEDGE_DATA_DIR=$DATA_DIR
ExecStart=/usr/bin/python3 $PREFIX/app/main.py --data-dir $DATA_DIR
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable wxedge-manager >/dev/null 2>&1 || true
  systemctl restart wxedge-manager
  sleep 1
  systemctl --no-pager --lines=3 status wxedge-manager || true
else
  echo "==> 4/4 已跳过 systemd 服务安装"
  echo "    手动启动: wxedge-manager --sim     # 或直接 wxedge-manager（使用真实 Docker）"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "安装完成 ✅"
echo "  WebUI : http://${IP:-127.0.0.1}:$PORT"
echo "  数据   : $DATA_DIR"
echo "  日志   : journalctl -u wxedge-manager -f"
echo
echo "安全提示：面板默认可被局域网访问。请在「系统设置」中设置访问令牌，公网访问请加反向代理 + HTTPS。"
