#!/usr/bin/env bash
# Slow, network-free integration test. Always starts with an empty environment.
set -euo pipefail
if [ "${1:-}" != --isolated ]; then
  exec /usr/bin/env -i PATH=/usr/bin:/bin /bin/bash "$0" --isolated
fi
fail() { echo "FAIL: $*" >&2; exit 1; }
for dependency in python3 openssl curl tar mktemp find mkdir rm bash env mv sleep cat; do
  command -v "$dependency" >/dev/null || fail "缺少依赖: $dependency (PATH=$PATH)"
done
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tmp="$(mktemp -d /tmp/fusion-clean-install.XXXXXXXX)"
pid=''
cleanup() {
  if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi
  rm -rf "$tmp"
}
trap cleanup EXIT
export HOME="$tmp/user" TMPDIR="$tmp/tmp"
export XDG_CONFIG_HOME="$HOME/.config" XDG_CACHE_HOME="$HOME/.cache" XDG_DATA_HOME="$HOME/.local/share"
mkdir -p "$HOME" "$TMPDIR" "$tmp/source"
tar -C "$repo" --exclude=.git --exclude=.venv --exclude=venv --exclude=__pycache__ \
  --exclude=node_modules --exclude=var --exclude=instance --exclude=fusion/evidence -cf - . |
  tar -C "$tmp/source" -xf -
cd "$tmp/source"
if [ ! -f COMMIT ]; then
  command -v git >/dev/null || fail '源码未带 COMMIT，且 PATH 中缺少 git'
  git -C "$repo" rev-parse --short=12 HEAD > COMMIT || fail '缺少 git/COMMIT 来源元数据'
fi
python3 -m venv "$tmp/venv" || fail 'python3 venv/ensurepip 不可用，请安装系统 python3-venv'
"$tmp/venv/bin/python" -I -m pip --isolated install --no-index --no-deps \
  --disable-pip-version-check --no-cache-dir -r requirements.lock
export PATH="$tmp/venv/bin:/usr/bin:/bin"
bash fusion/install.sh --home "$HOME/instance" --prefix "$HOME/bin"
export PATH="$HOME/bin:$PATH"
cd "$tmp"
mv "$tmp/source" "$tmp/detached-source"
fusion --version
fusion doctor --home "$HOME/instance"
fusion serve --home "$HOME/instance/db" --port 0 > "$tmp/server.log" 2>&1 &
pid=$!
port=''
for ((attempt=0; attempt<100; attempt++)); do
  kill -0 "$pid" 2>/dev/null || { cat "$tmp/server.log"; fail '服务提前退出'; }
  port="$(python3 -c 'import pathlib,re,sys; s=pathlib.Path(sys.argv[1]).read_text(); m=re.search(r"http://127.0.0.1:(\d+)",s); print(m[1] if m else "")' "$tmp/server.log")"
  if [ -n "$port" ] && [ "$(curl --noproxy '*' --max-time 2 -s -o "$tmp/health.json" -w '%{http_code}' "http://127.0.0.1:$port/api/health")" = 200 ]; then break; fi
  sleep 0.1
done
[ -n "$port" ] || fail '服务未输出端口'
[ "$(curl --noproxy '*' --max-time 2 -s -o "$tmp/health.json" -w '%{http_code}' "http://127.0.0.1:$port/api/health")" = 200 ] || fail 'health 非 200'
echo 'PASS health HTTP 200'
kill "$pid"
wait "$pid" 2>/dev/null || true
! kill -0 "$pid" 2>/dev/null || fail '遗留服务进程'
pid=''
bash "$tmp/detached-source/fusion/uninstall.sh" --home "$HOME/instance" --prefix "$HOME/bin" --yes
[ ! -e "$HOME/instance" ] && [ ! -e "$HOME/bin/fusion" ] || fail '遗留实例或启动器'
leftovers="$(find "$HOME" -mindepth 1 ! -path "$HOME/bin")"
[ -z "$leftovers" ] || fail "venv 外遗留配置: $leftovers"
python3 - "$HOME/instance" <<'PY'
from pathlib import Path
import os, sys
for process in Path('/proc').iterdir():
    if not process.name.isdigit() or int(process.name) == os.getpid():
        continue
    try:
        cmd = (process / 'cmdline').read_bytes().split(b'\0')
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        continue
    assert not any(x.decode(errors='replace').startswith(sys.argv[1]) for x in cmd), f'遗留进程 {process.name}'
PY
echo 'PASS clean install, isolated venv, doctor, health, uninstall, no config/process residue'
