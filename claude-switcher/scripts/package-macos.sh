#!/bin/bash
# Claude 切换器 - macOS 打包脚本（本地与 CI 共用）
# 用法: bash scripts/package-macos.sh [版本号]
# 产物: build/ClaudeSwitcher-<版本>-macos-<架构>.zip
#       build/ClaudeSwitcher-<版本>-macos-<架构>.dmg
#       build/ClaudeSwitcher-<版本>-installer-kit.zip
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-$(cat "$ROOT/VERSION")}"
PLIST_VERSION="${VERSION%%-*}"
if [ "$VERSION" != "$(cat "$ROOT/VERSION")" ]; then echo "VERSION 与组件清单版本不一致，请先更新 VERSION 并运行 scripts/gen-sums.sh" >&2; exit 1; fi
python3 "$ROOT/core/component-manager.py" verify --source "$ROOT"
case "$VERSION" in
    ''|*[!0-9A-Za-z._-]*)
        echo "✗ 版本号只能包含字母、数字、点、下划线和连字符: $VERSION" >&2
        exit 1
        ;;
esac

APP_NAME="Claude切换器"
BUILD="$ROOT/build"
APP="$BUILD/$APP_NAME.app"
ARCH="${CLAUDE_SWITCHER_BUILD_ARCH:-$(uname -m)}"
case "$ARCH" in
    arm64)  ARCH_LABEL="arm64" ;;
    x86_64) ARCH_LABEL="x86_64" ;;
    universal) ARCH_LABEL="universal" ;;
    *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac
ZIP="$BUILD/ClaudeSwitcher-$VERSION-macos-$ARCH_LABEL-app-only.zip"
DMG="$BUILD/ClaudeSwitcher-$VERSION-macos-$ARCH_LABEL-app-only.dmg"
KIT_NAME="ClaudeSwitcher-$VERSION"
KIT_ZIP="$BUILD/ClaudeSwitcher-$VERSION-installer-kit.zip"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/claude-switcher-package.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

mkdir -p "$BUILD"
rm -rf "$APP" "$ZIP" "$DMG" "$KIT_ZIP"

echo "==> 编译 $APP_NAME $VERSION ($ARCH_LABEL)"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
compile_arch() {
    xcrun swiftc -O -parse-as-library -warnings-as-errors \
        -target "$1-apple-macos13.0" "$ROOT/app/main.swift" -o "$2" \
        -framework SwiftUI -framework AppKit
}
if [ "$ARCH" = "universal" ]; then
    compile_arch arm64 "$WORK/ClaudeSwitcher-arm64"
    compile_arch x86_64 "$WORK/ClaudeSwitcher-x86_64"
    lipo -create "$WORK/ClaudeSwitcher-arm64" "$WORK/ClaudeSwitcher-x86_64" -output "$APP/Contents/MacOS/ClaudeSwitcher"
else
    compile_arch "$ARCH" "$APP/Contents/MacOS/ClaudeSwitcher"
fi

echo "==> 生成图标"
ICONSET="$WORK/AppIcon.iconset"
mkdir -p "$ICONSET"
for SZ in 16 32 128 256 512; do
    sips -z "$SZ" "$SZ" "$ROOT/assets/icon-2048.png" \
        --out "$ICONSET/icon_${SZ}x${SZ}.png" >/dev/null
    sips -z $((SZ * 2)) $((SZ * 2)) "$ROOT/assets/icon-2048.png" \
        --out "$ICONSET/icon_${SZ}x${SZ}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

echo "==> 写入 Info.plist"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleExecutable</key><string>ClaudeSwitcher</string>
	<key>CFBundleIdentifier</key><string>local.claude-switcher</string>
	<key>CFBundleName</key><string>Claude切换器</string>
	<key>CFBundleDisplayName</key><string>Claude切换器</string>
	<key>CFBundlePackageType</key><string>APPL</string>
	<key>CFBundleShortVersionString</key><string>$PLIST_VERSION</string>
	<key>CFBundleVersion</key><string>$PLIST_VERSION</string>
	<key>ClaudeSwitcherVersion</key><string>$VERSION</string>
	<key>LSMinimumSystemVersion</key><string>13.0</string>
	<key>CFBundleIconFile</key><string>AppIcon</string>
	<key>NSHighResolutionCapable</key><true/>
	<key>LSUIElement</key><true/>
</dict>
</plist>
EOF
plutil -lint "$APP/Contents/Info.plist" >/dev/null

IDENTITY="${CLAUDE_SWITCHER_SIGN_IDENTITY:--}"
SIGN_ARGS=(--force --deep -s "$IDENTITY")
if [ "$IDENTITY" != "-" ]; then SIGN_ARGS+=(--options runtime --timestamp); fi
codesign "${SIGN_ARGS[@]}" "$APP"
codesign --verify --deep --strict "$APP"
echo "✓ 签名与结构校验通过（未配置 NOTARY_PROFILE 时未经公证）"
if [ -n "${CLAUDE_SWITCHER_NOTARY_PROFILE:-}" ]; then
    [ "$IDENTITY" != "-" ] || { echo "公证必须使用 Developer ID 签名" >&2; exit 1; }
    ditto -c -k --keepParent "$APP" "$WORK/notary.zip"
    xcrun notarytool submit "$WORK/notary.zip" --keychain-profile "$CLAUDE_SWITCHER_NOTARY_PROFILE" --wait
    xcrun stapler staple "$APP"
    xcrun stapler validate "$APP"
fi

NOTE="$WORK/安装说明.txt"
cat > "$NOTE" <<'NOTE_EOF'
这是仅包含 macOS 界面 App 的安装包，不含核心脚本与守护进程。

首次使用请下载 installer-kit.zip，解压后双击 installer/安装.command。
若已安装过核心脚本，可把本 App 拖入「应用程序」覆盖旧版界面。

未安装核心脚本时，App 会提示「还差一步：安装核心脚本」并提供下载入口。
NOTE_EOF

echo "==> 生成 macOS zip(app-only)"
APP_STAGE="$WORK/appstage"
mkdir -p "$APP_STAGE"
ditto "$APP" "$APP_STAGE/$APP_NAME.app"
cp "$NOTE" "$APP_STAGE/安装说明.txt"
ditto -c -k --norsrc --noextattr "$APP_STAGE" "$ZIP"

echo "==> 生成 DMG(app-only)"
DMG_STAGE="$WORK/dmg"
mkdir -p "$DMG_STAGE"
ditto "$APP" "$DMG_STAGE/$APP_NAME.app"
cp "$NOTE" "$DMG_STAGE/安装说明.txt"
ln -s /Applications "$DMG_STAGE/Applications"
hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$DMG_STAGE" \
    -ov -format UDZO "$DMG" >/dev/null

echo "==> 生成 installer kit"
KIT_ROOT="$WORK/$KIT_NAME"
mkdir -p "$KIT_ROOT"
python3 - "$ROOT" "$KIT_ROOT" <<'PY_KIT'
from pathlib import Path
import json, shutil, sys
root, kit = map(Path, sys.argv[1:])
manifest = json.loads((root / 'COMPONENTS.json').read_text())
paths = list(manifest['files']) + ['COMPONENTS.json', 'SHA256SUMS', 'README.md', 'README.en.md', 'AGENTS.md', 'SECURITY.md']
paths += [p.relative_to(root).as_posix() for p in (root / 'docs').rglob('*') if p.is_file() and p.suffix in ('.md', '.json')]
for relative in paths:
    src, dst = root / relative, kit / relative
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
PY_KIT
chmod +x "$KIT_ROOT/installer/安装.command" "$KIT_ROOT/installer/卸载.command"
python3 "$KIT_ROOT/core/component-manager.py" verify --source "$KIT_ROOT"
ditto -c -k --keepParent --norsrc --noextattr "$KIT_ROOT" "$KIT_ZIP"

echo ""
SOURCE_ZIP="$BUILD/ClaudeSwitcher-$VERSION-source.zip"
python3 "$ROOT/scripts/package-source.py" --output "$SOURCE_ZIP"
echo "✓ 打包完成:"
for FILE in "$ZIP" "$DMG" "$KIT_ZIP" "$SOURCE_ZIP"; do
    [ -f "$FILE" ] || { echo "✗ 缺少产物 $FILE" >&2; exit 1; }
    printf '  %-58s %8s\n' "$(basename "$FILE")" "$(du -h "$FILE" | cut -f1)"
done
