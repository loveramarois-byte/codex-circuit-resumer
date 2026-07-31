#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
DESTINATION="${1:-$PROJECT_DIR/dist/Codex熔断续聊.app}"
DESTINATION="${DESTINATION:A}"
CONTENTS="$DESTINATION/Contents"
RESOURCES="$CONTENTS/Resources"
MACOS="$CONTENTS/MacOS"
ICONSET="$PROJECT_DIR/.build/CodexCircuitResumer.iconset"
CIRCUIT_BUILD_CACHE="${TMPDIR:-/tmp}/codex-circuit-resumer-build-cache"
APP_EXECUTABLE="$DESTINATION/Contents/MacOS/CodexCircuitResumer"

app_is_running() {
  /bin/ps -axo command= | /usr/bin/awk -v target="$APP_EXECUTABLE" '
    $0 == target || index($0, target " ") == 1 { found = 1 }
    END { exit(found ? 0 : 1) }
  '
}

was_running=0
if app_is_running; then
  was_running=1
fi

mkdir -p "${DESTINATION:h}"
mkdir -p "$CIRCUIT_BUILD_CACHE"
export CLANG_MODULE_CACHE_PATH="$CIRCUIT_BUILD_CACHE/clang"
if [[ -e "$DESTINATION" ]]; then
  safe_old="${DESTINATION}.old.$(date +%Y%m%d-%H%M%S)"
  mv "$DESTINATION" "$safe_old"
fi

mkdir -p "$MACOS" "$RESOURCES" "$ICONSET"
xcrun swiftc -parse-as-library -O -target arm64-apple-macosx13.0 \
  -framework SwiftUI -framework AppKit \
  "$PROJECT_DIR/app/CodexCircuitResumerApp.swift" \
  -o "$MACOS/CodexCircuitResumer"

cat > "$CONTENTS/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleDevelopmentRegion</key><string>zh_CN</string>
  <key>CFBundleDisplayName</key><string>Codex 熔断续聊</string>
  <key>CFBundleExecutable</key><string>CodexCircuitResumer</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleIdentifier</key><string>local.codex.circuitresumer</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>Codex 熔断续聊</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>2.5.7</string>
  <key>CFBundleVersion</key><string>257</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
</dict></plist>
PLIST
printf 'APPL????' > "$CONTENTS/PkgInfo"

for spec in "16 icon_16x16" "32 icon_16x16@2x" "32 icon_32x32" "64 icon_32x32@2x" "128 icon_128x128" "256 icon_128x128@2x" "256 icon_256x256" "512 icon_256x256@2x" "512 icon_512x512" "1024 icon_512x512@2x"; do
  set -- ${(s: :)spec}
  xcrun swift "$PROJECT_DIR/scripts/make_icon.swift" "$1" "$ICONSET/$2.png"
done
if ! iconutil -c icns "$ICONSET" -o "$RESOURCES/AppIcon.icns"; then
  fallback_icon=""
  if [[ ! -f "$RESOURCES/AppIcon.icns" ]]; then
    fallback_icon=$(find "$PROJECT_DIR/dist" -path '*/Contents/Resources/AppIcon.icns' -print -quit 2>/dev/null || true)
  fi
  if [[ -n "$fallback_icon" && -f "$fallback_icon" && "$fallback_icon" != "$RESOURCES/AppIcon.icns" ]]; then
    cp "$fallback_icon" "$RESOURCES/AppIcon.icns"
  fi
fi

cp "$PROJECT_DIR/scripts/control.sh" "$RESOURCES/control.sh"
cp "$PROJECT_DIR/src/daemon.py" "$RESOURCES/daemon.py"
cp "$PROJECT_DIR/config.example.json" "$RESOURCES/config.example.json"
chmod 755 "$RESOURCES/control.sh" "$RESOURCES/daemon.py"
/usr/bin/codesign --force --deep --sign - "$DESTINATION" >/dev/null
/usr/bin/xattr -cr "$DESTINATION" 2>/dev/null || true

# Replacing an app bundle does not replace an already running process. Restart
# only when this exact destination was open before the build, so the user sees
# the new UI immediately without touching the background LaunchAgent.
if (( was_running )); then
  /usr/bin/osascript -e 'tell application id "local.codex.circuitresumer" to quit' >/dev/null 2>&1 || true
  for _ in {1..50}; do
    if ! app_is_running; then
      break
    fi
    /bin/sleep 0.1
  done
  /usr/bin/open "$DESTINATION"
fi

echo "$DESTINATION"
