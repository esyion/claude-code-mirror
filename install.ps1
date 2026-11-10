param(
    [Parameter(Position=0)]
    [ValidatePattern('^(stable|latest|\d+\.\d+\.\d+(-[^\s]+)?)$')]
    [string]$Target = "latest"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = 'SilentlyContinue'

# ============================================================
#  Claude Code 安装脚本 - 镜像版
#  把 $MIRROR 改成你自己的 Cloudflare 域名即可
#  用法: irm https://<你的域名>/install.ps1 | iex
# ============================================================
$MIRROR = "https://claude-code-mirror.qingbo-my.workers.dev"
$DOWNLOAD_BASE_URL = "$MIRROR/claude-code-releases"

# Check for 32-bit Windows
if (-not [Environment]::Is64BitProcess) {
    Write-Error "Claude Code does not support 32-bit Windows. Please use a 64-bit version of Windows."
    exit 1
}

$DOWNLOAD_DIR = "$env:USERPROFILE\.claude\downloads"

# Use native ARM64 binary on ARM64 Windows, x64 otherwise
if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") {
    $platform = "win32-arm64"
} else {
    $platform = "win32-x64"
}
New-Item -ItemType Directory -Force -Path $DOWNLOAD_DIR | Out-Null

# Always download latest version (which has the most up-to-date installer)
try {
    $version = Invoke-RestMethod -Uri "$DOWNLOAD_BASE_URL/latest" -ErrorAction Stop
}
catch {
    Write-Error "Failed to get latest version from mirror (${MIRROR}): $_"
    exit 1
}

# Reject non-version content (e.g. an HTML error page) before it reaches the manifest URL
if ($version -notmatch '^\d+\.\d+\.\d+') {
    Write-Error "Failed to get a valid version from mirror (${MIRROR}). Check https://<你的域名>/claude-code-releases/mirror-status.json for sync status."
    exit 1
}

try {
    $manifest = Invoke-RestMethod -Uri "$DOWNLOAD_BASE_URL/$version/manifest.json" -ErrorAction Stop
    $checksum = $manifest.platforms.$platform.checksum

    if (-not $checksum) {
        Write-Error "Platform $platform not found in manifest"
        exit 1
    }
}
catch {
    Write-Error "Failed to get manifest: $_"
    exit 1
}

# Download and verify
$binaryPath = "$DOWNLOAD_DIR\claude-$version-$platform.exe"
try {
    Invoke-WebRequest -Uri "$DOWNLOAD_BASE_URL/$version/$platform/claude.exe" -OutFile $binaryPath -ErrorAction Stop
}
catch {
    Write-Error "Failed to download binary: $_"
    if (Test-Path $binaryPath) {
        Remove-Item -Force $binaryPath
    }
    exit 1
}

# Calculate checksum (仍按官方 manifest 的 SHA256 校验, 防止镜像文件损坏/被篡改)
$actualChecksum = (Get-FileHash -Path $binaryPath -Algorithm SHA256).Hash.ToLower()

if ($actualChecksum -ne $checksum) {
    Write-Error "Checksum verification failed"
    Remove-Item -Force $binaryPath
    exit 1
}

# Run claude install to set up launcher and shell integration
Write-Output "Setting up Claude Code..."
try {
    if ($Target) {
        & $binaryPath install $Target
    }
    else {
        & $binaryPath install
    }
    # Native exit codes don't trigger $ErrorActionPreference - capture explicitly
    $installExitCode = $LASTEXITCODE
}
finally {
    try {
        # Clean up downloaded file
        # Wait a moment for any file handles to be released
        Start-Sleep -Seconds 1
        Remove-Item -Force $binaryPath
    }
    catch {
        Write-Warning "Could not remove temporary file: $binaryPath"
    }
}

if ($installExitCode -ne 0) {
    Write-Error "Installation failed (exit code $installExitCode)"
    exit $installExitCode
}

Write-Output ""
Write-Output "$([char]0x2705) Installation complete!"
Write-Output ""
