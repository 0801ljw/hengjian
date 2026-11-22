#!/usr/bin/env bash
# Fusion 卸载脚本：移除启动器与实例目录（默认需要确认）
set -euo pipefail
FUSION_HOME="${FUSION_HOME:-$HOME/.fusion}"
PREFIX="${PREFIX:-$HOME/.local/bin}"
YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --home) FUSION_HOME="$2"; shift ;;
    --prefix) PREFIX="$2"; shift ;;
    --yes) YES=1 ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac; shift
done

echo "将移除："
echo "  启动器: $PREFIX/fusion"
echo "  实例  : $FUSION_HOME"
if [ "$YES" != "1" ]; then
  printf '确认？(yes/no) '; read -r ans
  [ "$ans" = "yes" ] || { echo "已取消"; exit 1; }
fi

[ -f "$PREFIX/fusion" ] && rm -f "$PREFIX/fusion" && echo "已删除启动器"
if [ -d "$FUSION_HOME" ]; then
  # 私钥提醒：删除前提示是否备份
  if [ -f "$FUSION_HOME/board-private.pem" ]; then
    echo "⚠ 检测到 board 私钥，删除后不可恢复（如需保留请先备份）"
  fi
  rm -rf "$FUSION_HOME" && echo "已删除实例目录"
fi
echo "卸载完成（仓库目录未改动，可手动删除）"
