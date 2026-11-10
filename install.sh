#!/usr/bin/env bash
# ============================================================
#  Claude Code 安装脚本 - 镜像版 (macOS / Linux)
#  把 MIRROR 改成你自己的 Cloudflare 域名即可
#  用法: curl -fsSL https://<你的域名>/install.sh | bash
# ============================================================
set -euo pipefail

MIRROR="https://mirror.example.com"
DOWNLOAD_BASE_URL="$MIRROR/claude-code-releases"
TARGET="${1:-latest}"

# ---- 平台识别 --------------------------------------------------
OS="$(uname -s)"
ARCH="$(uname -m)"

case "${OS}_${ARCH}" in
    Darwin_arm64)  PLATFORM="darwin-arm64" ;;
    Darwin_x86_64) PLATFORM="darwin-x64" ;;
    Linux_aarch64) PLATFORM="linux-arm64" ;;
    Linux_x86_64)  PLATFORM="linux-x64" ;;
    *) echo "Unsupported platform: ${OS} ${ARCH}" >&2; exit 1 ;;
esac

# musl (Alpine 等) 检测
if [ -e "/lib/ld-musl-${ARCH}.so.1" ]; then
    case "$PLATFORM" in
        linux-arm64) PLATFORM="linux-arm64-musl" ;;
        linux-x64)   PLATFORM="linux-x64-musl" ;;
    esac
fi

# ---- 获取版本与 manifest (无 jq 依赖, awk 解析) ------------------
VERSION="$(curl -fsSL "$DOWNLOAD_BASE_URL/latest")"
case "$VERSION" in
    "" | *[!0-9a-zA-Z.-]*)
        echo "Failed to get a valid version from mirror ($MIRROR)" >&2
        exit 1
        ;;
esac

CHECKSUM="$(curl -fsSL "$DOWNLOAD_BASE_URL/$VERSION/manifest.json" | awk -v p="\"$PLATFORM\"" '
    index($0, p) { inblock = 1 }
    inblock && /"checksum"/ { gsub(/.*"checksum":[[:space:]]*"/, ""); gsub(/".*/, ""); print; exit }
')"

if [ -z "$CHECKSUM" ]; then
    echo "Platform $PLATFORM not found in manifest $VERSION" >&2
    exit 1
fi

# ---- 下载并校验 --------------------------------------------------
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
BINARY_PATH="$TMP_DIR/claude-$VERSION-$PLATFORM"

echo "Downloading Claude Code $VERSION ($PLATFORM)..."
curl -fsSL "$DOWNLOAD_BASE_URL/$VERSION/$PLATFORM/claude" -o "$BINARY_PATH"

if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL="$(sha256sum "$BINARY_PATH" | awk '{print $1}')"
else
    ACTUAL="$(shasum -a 256 "$BINARY_PATH" | awk '{print $1}')"
fi
ACTUAL="$(printf '%s' "$ACTUAL" | tr 'A-Z' 'a-z')"

if [ "$ACTUAL" != "$CHECKSUM" ]; then
    echo "Checksum verification failed" >&2
    exit 1
fi

# ---- 安装 --------------------------------------------------------
chmod +x "$BINARY_PATH"
echo "Setting up Claude Code..."
"$BINARY_PATH" install "$TARGET"

echo ""
echo "✅ Installation complete!"
echo ""
