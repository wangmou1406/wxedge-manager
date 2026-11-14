#!/usr/bin/env bash
# WXEdge 魔方管理器 · 在线安装引导
# 用法:
#   curl -fsSL https://raw.githubusercontent.com/wangmou1406/wxedge-manager/main/install-online.sh | sudo bash
#   curl -fsSL ... | sudo bash -s -- --port 9000
# 可选环境变量:
#   WXEDGE_REPO=owner/repo        默认 wangmou1406/wxedge-manager
#   WXEDGE_REF=main               默认 main，也可填 tag / 分支
#   WXEDGE_SRC_URL=<tar.gz 地址>   自定义下载地址（镜像 / 离线源）
set -euo pipefail

REPO="${WXEDGE_REPO:-wangmou1406/wxedge-manager}"
REF="${WXEDGE_REF:-main}"
SRC_URL="${WXEDGE_SRC_URL:-https://codeload.github.com/${REPO}/tar.gz/refs/heads/${REF}}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请用 root 或 sudo 运行：" >&2
  echo "  curl -fsSL https://raw.githubusercontent.com/${REPO}/main/install-online.sh | sudo bash" >&2
  exit 1
fi

need_pkgs=()
command -v curl >/dev/null 2>&1 || need_pkgs+=(curl)
command -v tar >/dev/null 2>&1 || need_pkgs+=(tar)
if ! dpkg -s ca-certificates >/dev/null 2>&1; then need_pkgs+=(ca-certificates); fi
if [[ ${#need_pkgs[@]} -gt 0 ]]; then
  if command -v apt-get >/dev/null 2>&1; then
    echo "==> 安装下载依赖: ${need_pkgs[*]}"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${need_pkgs[@]}"
  else
    echo "缺少 ${need_pkgs[*]}，请先手动安装" >&2
    exit 1
  fi
fi

tmp_dir="$(mktemp -d)"
cleanup() { rm -rf "$tmp_dir"; }
trap cleanup EXIT

echo "==> 下载源码: $SRC_URL"
curl -fL --retry 3 --connect-timeout 15 -o "$tmp_dir/src.tar.gz" "$SRC_URL"
tar -xzf "$tmp_dir/src.tar.gz" -C "$tmp_dir"
src_dir="$(find "$tmp_dir" -maxdepth 2 -type d -name 'wxedge-manager-*' | head -n 1)"
if [[ -z "$src_dir" || ! -f "$src_dir/install.sh" ]]; then
  echo "下载包里没有 install.sh，请检查仓库地址或网络" >&2
  exit 1
fi

echo "==> 运行安装器"
bash "$src_dir/install.sh" "$@"
