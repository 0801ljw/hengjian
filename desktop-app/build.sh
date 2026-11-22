#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$ROOT/build/Hengjian.app"
BIN="$APP/Contents/MacOS/Hengjian"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$ROOT/Info.plist" "$APP/Contents/Info.plist"
cp -R "$ROOT/runtime" "$APP/Contents/Resources/runtime"
if [ -x "$ROOT/dist/fusion-server" ]; then
  mkdir -p "$APP/Contents/Resources/bin"
  cp "$ROOT/dist/fusion-server" "$APP/Contents/Resources/bin/fusion-server"
  chmod 0755 "$APP/Contents/Resources/bin/fusion-server"
fi

swiftc -O -swift-version 5 -target arm64-apple-macosx13.0 \
  -framework Cocoa -framework WebKit \
  "$ROOT/Sources/HengjianApp.swift" \
  -o "$BIN"

codesign --force --deep --sign - "$APP"
echo "built: $APP"
