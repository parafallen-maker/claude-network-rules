#!/bin/bash
# Build a complete macOS App with verified first-install resources.
# Usage: CLAUDE_SWITCHER_BUILD_ARCH=universal bash scripts/package-macos.sh [version]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VERSION="${1:-$(cat "$ROOT/VERSION")}"
if [ "$VERSION" != "$(cat "$ROOT/VERSION")" ]; then
    echo "VERSION 与组件清单版本不一致，请先更新 VERSION 并运行 scripts/gen-sums.sh" >&2
    exit 1
fi
case "$VERSION" in
    ''|*[!0-9A-Za-z._-]*) echo "版本号只能包含字母、数字、点、下划线和连字符" >&2; exit 1 ;;
esac
python3 -B "$ROOT/core/component-manager.py" verify --source "$ROOT"

APP_NAME="Claude切换器"
BUILD="$ROOT/build"
APP="$BUILD/$APP_NAME.app"
ARCH="${CLAUDE_SWITCHER_BUILD_ARCH:-$(uname -m)}"
case "$ARCH" in
    arm64|x86_64|universal) ;;
    *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac
ZIP="$BUILD/ClaudeSwitcher-$VERSION-macos-$ARCH.zip"
DMG="$BUILD/ClaudeSwitcher-$VERSION-macos-$ARCH.dmg"
KIT_ZIP="$BUILD/ClaudeSwitcher-$VERSION-installer-kit.zip"
SOURCE_ZIP="$BUILD/ClaudeSwitcher-$VERSION-source.zip"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/claude-switcher-package.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p "$BUILD"
rm -rf "$APP" "$ZIP" "$DMG" "$KIT_ZIP" "$SOURCE_ZIP"

echo "==> 编译完整 App $VERSION ($ARCH)"
python3 -B - "$ROOT" "$APP" "$VERSION" "$ARCH" <<'PY_BUILD'
import importlib.util
from pathlib import Path
import sys
root, output = map(Path, sys.argv[1:3])
spec = importlib.util.spec_from_file_location("installer", root / "installer/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
installer.compile_app(root, output, sys.argv[3], arch=sys.argv[4])
PY_BUILD

echo "==> 内置已校验的首次安装组件"
python3 -B "$ROOT/scripts/package-source.py" --kit --directory "$APP/Contents/Resources/InstallerSource"
python3 -B - "$ROOT" "$APP" "$VERSION" <<'PY_INFO'
import hashlib, json, sys
from pathlib import Path
root, app = map(Path, sys.argv[1:3])
info = {"version": sys.argv[3], "components_sha256": hashlib.sha256((root / "COMPONENTS.json").read_bytes()).hexdigest()}
(app / "Contents/Resources/BuildInfo.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
PY_INFO

# Re-sign inside out after embedding resources. Both binaries must use the
# configured identity; an ad-hoc nested helper cannot be notarized later.
IDENTITY="${CLAUDE_SWITCHER_SIGN_IDENTITY:--}"
SIGN_ARGS=(--force -s "$IDENTITY")
if [ "$IDENTITY" != "-" ]; then SIGN_ARGS+=(--options runtime --timestamp); fi
codesign "${SIGN_ARGS[@]}" "$APP/Contents/Helpers/NetworkNotifier.app"
codesign "${SIGN_ARGS[@]}" "$APP"
codesign --verify --deep --strict "$APP"
plutil -lint "$APP/Contents/Info.plist" >/dev/null
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
此 App 已包含核心组件、守护进程安装器和网络通知助手。

1. 将 Claude切换器.app 拖入「应用程序」，然后打开。
2. 首次使用点击「安装本机组件」，按终端引导确认代理与地区并授权安装。
3. 安装过程验证预编译 App 和内置组件，完成检查后才提交；失败保留或恢复旧版本。

预编译 App 安装需要 Python 3，不需要重新编译 Swift。
源码/installer-kit 安装需要 Apple 命令行开发工具、Swift 与 Python 3。
默认发布包使用 ad-hoc 签名，未经 Apple 公证；首次打开可能被 macOS 拦截。
账号目录、代理凭据和浏览器资料不会随 App 分发。
NOTE_EOF

echo "==> 生成完整 macOS ZIP"
APP_STAGE="$WORK/appstage"
mkdir -p "$APP_STAGE"
ditto "$APP" "$APP_STAGE/$APP_NAME.app"
cp "$NOTE" "$APP_STAGE/安装说明.txt"
ditto -c -k --norsrc --noextattr "$APP_STAGE" "$ZIP"

echo "==> 生成完整 macOS DMG"
DMG_STAGE="$WORK/dmg"
mkdir -p "$DMG_STAGE"
ditto "$APP" "$DMG_STAGE/$APP_NAME.app"
cp "$NOTE" "$DMG_STAGE/安装说明.txt"
ln -s /Applications "$DMG_STAGE/Applications"
hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$DMG_STAGE" -ov -format UDZO "$DMG" >/dev/null

echo "==> 生成源码与 installer kit"
python3 -B "$ROOT/scripts/package-source.py" --kit --output "$KIT_ZIP"
python3 -B "$ROOT/scripts/package-source.py" --output "$SOURCE_ZIP"
echo "✓ 打包完成:"
for FILE in "$ZIP" "$DMG" "$KIT_ZIP" "$SOURCE_ZIP"; do
    [ -f "$FILE" ] || { echo "缺少产物 $FILE" >&2; exit 1; }
    printf '  %-58s %8s\n' "$(basename "$FILE")" "$(du -h "$FILE" | cut -f1)"
done
