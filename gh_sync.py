#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 版同步脚本: downloads.claude.ai -> GitHub Releases

由 .github/workflows/sync.yml 定时触发(也可手动 workflow_dispatch):
  1. 拉取官方 latest/stable 指针与 manifest
  2. 下载 8 平台二进制, 按官方 SHA256 逐一校验
  3. 创建 GitHub Release (tag=版本号), 资产命名: <platform>.<binary> + manifest.json
  4. 补齐缺失的中间版本 (npm 版本列表枚举, 每次最多 BACKFILL 个, 每版本一个 Release)
  5. 更新 channels/latest, channels/stable 并提交 (Worker 从 raw 读取它们)
  6. 按保留策略删除旧 Release (RETENTION=0 表示永久保留全部)

依赖: gh CLI (ubuntu runner 自带) + GH_TOKEN (workflow 注入)
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.environ.get("GITHUB_REPOSITORY", "")
UPSTREAM = "https://downloads.claude.ai/claude-code-releases"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[^\s]+)?$")
RETENTION = int(os.environ.get("RETENTION", "8"))   # 0 = 永久保留全部版本
BACKFILL = int(os.environ.get("BACKFILL", "4"))     # 每次运行最多补齐的缺失版本数
UA = {"User-Agent": "claude-mirror-sync"}


def log(msg):
    print(f"[sync] {msg}", flush=True)


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


class NotFound(Exception):
    """上游 404 —— npm 存在该版本但没有原生构建 (无 manifest)."""


def http_get(url):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url)
            if attempt == 2:
                raise
            log(f"retry {url}: HTTP {e.code}")
            time.sleep(5 * (attempt + 1))
        except Exception as e:
            if attempt == 2:
                raise
            log(f"retry {url}: {e}")
            time.sleep(5 * (attempt + 1))


def upstream_pointer(name):
    text = http_get(f"{UPSTREAM}/{name}").decode().strip()
    if not VERSION_RE.match(text):
        raise RuntimeError(f"upstream /{name} returned invalid content: {text[:50]!r}")
    return text


def vkey(v):
    nums, _, suffix = v.partition("-")
    return (tuple(int(x) for x in nums.split(".")), suffix)


def npm_versions():
    """npm registry 与官方原生构建使用同一版本号, 用它枚举全部历史版本 (新→旧)."""
    data = json.loads(http_get("https://registry.npmjs.org/@anthropic-ai/claude-code"))
    return sorted((v for v in data["versions"] if VERSION_RE.match(v)), key=vkey, reverse=True)


def prune_floor(releases):
    """RETENTION>0 时只补齐保留窗口内(比最旧保留版更新)的版本, 避免同步完就被清理;
    RETENTION=0 时返回 None, 即全量补齐."""
    if RETENTION <= 0:
        return None
    kept = sorted((t for t in releases if VERSION_RE.match(t)), key=vkey, reverse=True)[:RETENTION]
    return vkey(kept[-1]) if kept else None


def existing_releases():
    out = gh("release", "list", "--json", "tagName", "--limit", "100")
    return {item["tagName"] for item in json.loads(out)}


def release_asset_names(tag):
    out = gh("api", f"repos/{REPO}/releases/tags/{tag}")
    return {a["name"] for a in json.loads(out)["assets"]}


def version_complete(version):
    """Release 已存在且资产齐全才算同步完成 (中途失败可重跑补齐)."""
    manifest = json.loads(http_get(f"{UPSTREAM}/{version}/manifest.json"))
    expected = {"manifest.json"} | {
        f"{p}.{i['binary']}" for p, i in manifest["platforms"].items()
    }
    try:
        return expected <= release_asset_names(version)
    except Exception:
        return False


def stage_version(version, tmpdir):
    """下载并校验一个版本, 返回 (manifest, [(本地路径, 资产名), ...])."""
    manifest = json.loads(http_get(f"{UPSTREAM}/{version}/manifest.json"))
    manifest_path = os.path.join(tmpdir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    staged = [(manifest_path, "manifest.json")]

    for platform, info in manifest["platforms"].items():
        binary = info["binary"]
        asset = f"{platform}.{binary}"
        path = os.path.join(tmpdir, asset)
        url = f"{UPSTREAM}/{version}/{platform}/{binary}"
        for attempt in range(3):
            sha = hashlib.sha256()
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
                    while True:
                        chunk = r.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        sha.update(chunk)
                if sha.hexdigest() != info["checksum"].lower():
                    raise RuntimeError("checksum mismatch")
                staged.append((path, asset))
                log(f"  {platform}: ok ({info['size'] / 1e6:.0f} MB)")
                break
            except Exception as e:
                if attempt == 2:
                    raise RuntimeError(f"{platform}: {e}")
                log(f"  retry {platform}: {e}")
    return manifest, staged


def publish(version, manifest, staged):
    if version not in existing_releases():
        notes = (f"Mirrored from downloads.claude.ai\n\n"
                 f"- buildDate: {manifest.get('buildDate', '?')}\n"
                 f"- commit: {manifest.get('commit', '?')}")
        args = ["release", "create", version, "--title", version, "--notes", notes]
        if "-" in version:
            args.append("--prerelease")
        gh(*args)
    gh("release", "upload", version, *[p for p, _ in staged], "--clobber")
    log(f"{version}: {len(staged)} release assets uploaded")


def update_channels(latest, stable):
    os.makedirs("channels", exist_ok=True)
    changed = []
    for name, value in (("latest", latest), ("stable", stable)):
        path = os.path.join("channels", name)
        old = open(path).read().strip() if os.path.exists(path) else ""
        if old != value:
            with open(path, "w") as f:
                f.write(value + "\n")
            changed.append(f"{name}: {old or '-'} -> {value}")
    if not changed:
        return
    for c in changed:
        log(f"channel {c}")
    for cmd in (
        ["git", "config", "user.name", "mirror-bot"],
        ["git", "config", "user.email", "actions@github.com"],
        ["git", "add", "channels"],
        ["git", "commit", "-m", "chore(channels): " + "; ".join(changed)],
        ["git", "push"],
    ):
        subprocess.run(cmd, check=True)


def prune(latest, stable):
    if RETENTION <= 0:
        log("RETENTION=0, keeping all releases")
        return
    tags = {t for t in existing_releases() if VERSION_RE.match(t)}
    keep = set(sorted(tags, key=vkey, reverse=True)[:RETENTION]) | {latest, stable}
    for v in sorted(tags - keep, key=vkey):
        log(f"delete old release {v}")
        gh("release", "delete", v, "--yes", "--cleanup-tag")


def main():
    if not REPO:
        die("GITHUB_REPOSITORY 未设置 (应在 GitHub Actions 中运行)")

    latest = upstream_pointer("latest")
    stable = upstream_pointer("stable")
    log(f"upstream: latest={latest} stable={stable}")

    releases = existing_releases()

    # 除 latest/stable 外, 补齐缺失的中间版本 (从新到旧, 每次最多 BACKFILL 个)
    targets = list(dict.fromkeys([latest, stable]))
    try:
        floor = prune_floor(releases)
        missing = [v for v in npm_versions()
                   if v not in releases and v not in targets
                   and (floor is None or vkey(v) > floor)]
        if missing:
            log(f"{len(missing)} missing versions found, backfill up to {BACKFILL}")
        targets += missing[:BACKFILL]
    except Exception as e:
        log(f"backfill enumeration failed, pointers only: {e}")

    failures = []
    for v in targets:
        try:
            if v in releases and version_complete(v):
                log(f"{v}: already synced")
                continue
            with tempfile.TemporaryDirectory(prefix="claude-mirror-") as tmp:
                manifest, staged = stage_version(v, tmp)
                publish(v, manifest, staged)
        except NotFound:
            log(f"{v}: no native build upstream, skipped")
        except Exception as e:
            failures.append(v)
            log(f"{v}: FAILED {e}")

    update_channels(latest, stable)

    # Releases 页面的 "Latest" 徽标跟随 latest 频道 (仅展示用途, Worker 不依赖它)
    try:
        gh("release", "edit", latest, "--latest")
    except Exception as e:
        log(f"mark latest badge: {e}")

    prune(latest, stable)

    if failures:
        die(f"sync finished with failures: {failures}")
    log("done")


if __name__ == "__main__":
    main()
