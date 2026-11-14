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
CHECK_SPACE=0
MIN_FREE_MB=2048

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
  --check-space       只检查安装目录、数据目录和 Docker 镜像目录的可用空间，不安装
  --min-free-mb N     空间检查阈值（单位 MB，默认 2048）
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
    --check-space) CHECK_SPACE=1; shift ;;
    --min-free-mb) MIN_FREE_MB="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 1 ;;
  esac
done

if ! [[ "$MIN_FREE_MB" =~ ^[0-9]+$ ]] || [[ "$MIN_FREE_MB" -lt 1 ]]; then
  echo "错误：--min-free-mb 需要是大于 0 的整数" >&2
  exit 1
fi

human_kb() {
  awk -v k="$1" 'BEGIN {
    if (k >= 1048576) printf "%.1f GB", k / 1048576;
    else if (k >= 1024) printf "%.0f MB", k / 1024;
    else printf "%d KB", k;
  }'
}

check_space() {
  local min_kb=$((MIN_FREE_MB * 1024))
  local fail=0 label target dir info avail total mount docker_root
  echo "==> 存储空间检测（建议至少 ${MIN_FREE_MB} MB 可用）"
  for entry in "安装目录:${PREFIX}" "数据目录:${DATA_DIR}"; do
    label="${entry%%:*}"
    target="${entry#*:}"
    dir="$target"
    while [[ ! -d "$dir" && "$dir" != "/" ]]; do dir="$(dirname "$dir")"; done
    info="$(df -Pk "$dir" 2>/dev/null | awk 'NR==2 {print $4, $2, $6}')"
    if [[ -z "$info" ]]; then
      echo "  ${label}  ${target}：无法读取磁盘信息" >&2
      fail=1
      continue
    fi
    read -r avail total mount <<< "$info"
    echo "  ${label}  ${target}"
    echo "            挂载点 ${mount}，可用 $(human_kb "$avail") / 共 $(human_kb "$total")"
    if (( avail < min_kb )); then
      fail=1
    fi
  done
  if command -v docker >/dev/null 2>&1; then
    docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
    if [[ -n "$docker_root" && -d "$docker_root" ]]; then
      read -r avail total mount <<< "$(df -Pk "$docker_root" 2>/dev/null | awk 'NR==2 {print $4, $2, $6}')"
      echo "  镜像目录  ${docker_root}"
      echo "            挂载点 ${mount}，可用 $(human_kb "$avail") / 共 $(human_kb "$total")（镜像与容器层）"
      if (( avail < min_kb )); then
        fail=1
      fi
    else
      echo "  镜像目录  Docker 未运行，跳过（拉取镜像前会占用该目录）"
    fi
  else
    echo "  镜像目录  未安装 Docker，跳过"
  fi
  echo "  说明：这里只算程序、Docker 镜像和系统开销；每个实例的硬盘配额另算。"
  if [[ "$fail" -eq 0 ]]; then
    echo "  结果：可用空间足够"
  else
    echo "  结果：可用空间低于建议值 ${MIN_FREE_MB} MB" >&2
  fi
  return "$fail"
}

if [[ "$CHECK_SPACE" -eq 1 ]]; then
  if check_space; then
    exit 0
  else
    exit 1
  fi
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -d "$SRC_DIR/app" ]]; then
  echo "错误：在 $SRC_DIR 下找不到 app 目录，请在项目根目录运行本脚本" >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "错误：请使用 root 或 sudo 运行（安装到 $PREFIX 并创建 systemd 服务）" >&2
  exit 1
fi

echo "==> 安装前检查存储空间"
if ! check_space; then
  echo "    可用空间低于建议值 ${MIN_FREE_MB} MB，继续安装；拉取镜像或写入实例数据时可能失败。" >&2
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
