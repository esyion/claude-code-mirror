#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code 官方发布镜像 -> RustFS 同步工具

定时把 https://downloads.claude.ai/claude-code-releases 的 8 个平台二进制
同步到 S3 兼容存储(RustFS)里, URL 结构与官方完全一致, 安装脚本只需换域名:

    官方:  https://downloads.claude.ai/claude-code-releases/<version>/<platform>/claude
    镜像:  https://<你的域名>/claude-code-releases/<version>/<platform>/claude

用法:
    python sync.py                # 同步 latest + stable, 按保留策略清理旧版本
    python sync.py --retention 5  # 只保留最近 5 个版本
    python sync.py --public       # 同时给 bucket 设置匿名只读策略(首次运行用)
    python sync.py --dry-run

环境变量:
    RUSTFS_ENDPOINT   S3 兼容 endpoint, 默认 http://127.0.0.1:9000 (走内网, 不要走 Cloudflare)
    RUSTFS_ACCESS_KEY 访问密钥 (必填)
    RUSTFS_SECRET_KEY 私有密钥 (必填)
    RUSTFS_BUCKET     bucket 名, 默认 claude-mirror
    MIRROR_BASE_PATH  对象前缀, 默认 claude-code-releases
    MIRROR_WORKERS    并发下载/上传线程数, 默认 4

依赖: pip install boto3 requests
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import ClientError

UPSTREAM = "https://downloads.claude.ai/claude-code-releases"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[^\s]+)?$")

ENDPOINT = os.environ.get("RUSTFS_ENDPOINT", "http://127.0.0.1:9000").rstrip("/")
ACCESS_KEY = os.environ.get("RUSTFS_ACCESS_KEY", "")
SECRET_KEY = os.environ.get("RUSTFS_SECRET_KEY", "")
BUCKET = os.environ.get("RUSTFS_BUCKET", "claude-mirror")
BASE_PATH = os.environ.get("MIRROR_BASE_PATH", "claude-code-releases").strip("/")
WORKERS = int(os.environ.get("MIRROR_WORKERS", "4"))

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"   # 版本化路径, 永不过期
SHORT_CACHE = "public, max-age=300"                       # latest/stable/安装脚本

_local = threading.local()


def log(msg):
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def key(*parts):
    return "/".join((BASE_PATH,) + tuple(p.strip("/") for p in parts if p))


def s3():
    """每个线程一个 client (boto3 client 线程安全性存疑, 按线程隔离最稳)."""
    if getattr(_local, "s3", None) is None:
        session = boto3.Session(
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
            region_name="us-east-1",
        )
        _local.s3 = session.client(
            "s3",
            endpoint_url=ENDPOINT,
            config=Config(
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=10,
                read_timeout=120,
            ),
        )
    return _local.s3


def http():
    if getattr(_local, "http", None) is None:
        _local.http = requests.Session()
    return _local.http


# ---------------------------------------------------------------- upstream --

def fetch_upstream(path):
    url = f"{UPSTREAM}/{path}"
    for attempt in range(3):
        try:
            r = http().get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            if attempt == 2:
                raise
            log(f"retry fetching /{path}: {e}")
            time.sleep(3 * (attempt + 1))


def upstream_pointer(name):
    text = fetch_upstream(name).strip()
    if not VERSION_RE.match(text):
        # 拿到 HTML 错误页等异常内容时拒绝入库
        raise RuntimeError(f"upstream /{name} returned invalid content: {text[:60]!r}")
    return text


def upstream_manifest(version):
    return json.loads(fetch_upstream(f"{version}/manifest.json"))


# ------------------------------------------------------------- s3 helpers --

def object_size(k):
    try:
        return s3().head_object(Bucket=BUCKET, Key=k).get("ContentLength")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def put_text(k, body, content_type, cache=SHORT_CACHE):
    s3().put_object(Bucket=BUCKET, Key=k, Body=body.encode("utf-8"),
                    ContentType=content_type, CacheControl=cache)


PUBLIC_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": "*",
        "Action": ["s3:GetObject"],
        "Resource": [f"arn:aws:s3:::{BUCKET}/*"],
    }],
}


def ensure_bucket(public):
    try:
        s3().head_bucket(Bucket=BUCKET)
    except ClientError:
        s3().create_bucket(Bucket=BUCKET)
        log(f"created bucket {BUCKET}")
    if public:
        s3().put_bucket_policy(Bucket=BUCKET, Policy=json.dumps(PUBLIC_POLICY))
        log("applied anonymous-read bucket policy")


# ------------------------------------------------------------------ sync --

def incomplete_platforms(version, manifest):
    """返回远端缺失或大小不符的平台列表 (大小一致视为已同步, 避免重复下 2.3GB)."""
    todo = []
    for platform, info in manifest["platforms"].items():
        size = object_size(key(version, platform, info["binary"]))
        if size != info["size"]:
            todo.append((platform, info))
    return todo


def sync_platform(version, platform, info, tmpdir):
    """下载单个平台二进制, 按官方 manifest 校验 SHA256 后上传."""
    binary = info["binary"]
    url = f"{UPSTREAM}/{version}/{platform}/{binary}"
    expected = info["checksum"].lower()
    path = os.path.join(tmpdir, f"{version}-{platform}-{binary}")

    for attempt in range(3):
        sha = hashlib.sha256()
        try:
            with http().get(url, stream=True, timeout=(15, 120)) as r:
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
                        sha.update(chunk)
            if sha.hexdigest() != expected:
                raise RuntimeError(f"checksum mismatch: got {sha.hexdigest()}")
            with open(path, "rb") as f:
                s3().put_object(Bucket=BUCKET, Key=key(version, platform, binary), Body=f,
                                ContentType="application/octet-stream",
                                CacheControl=IMMUTABLE_CACHE)
            return platform, info["size"]
        except Exception as e:
            if attempt == 2:
                raise RuntimeError(f"{platform}: {e}") from e
            log(f"retry {attempt + 1}/3 for {platform}: {e}")
        finally:
            if os.path.exists(path):
                os.remove(path)


def sync_version(version, dry_run):
    """同步一个版本. manifest 最后上传 —— 它存在即代表该版本完整可用."""
    manifest = upstream_manifest(version)
    todo = incomplete_platforms(version, manifest)
    total = len(manifest["platforms"])

    if not todo and object_size(key(version, "manifest.json")) is not None:
        log(f"{version}: already synced")
        return True

    if dry_run:
        log(f"{version}: needs {len(todo)}/{total} platform binaries")
        return not todo

    log(f"{version}: syncing {len(todo)}/{total} platforms ...")
    failed = []
    with tempfile.TemporaryDirectory(prefix="claude-mirror-") as tmp:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futs = {pool.submit(sync_platform, version, p, i, tmp): p for p, i in todo}
            for fut in as_completed(futs):
                p = futs[fut]
                try:
                    _, size = fut.result()
                    log(f"  {p}: ok ({size / 1e6:.0f} MB)")
                except Exception as e:
                    failed.append(p)
                    log(f"  {p}: FAILED {e}")

    if failed:
        log(f"{version}: incomplete, failed platforms: {failed}")
        return False

    put_text(key(version, "manifest.json"), json.dumps(manifest, indent=2),
             "application/json", IMMUTABLE_CACHE)
    log(f"{version}: done")
    return True


def upload_pointer(name, version, dry_run):
    if dry_run:
        return
    put_text(key(name), version + "\n", "text/plain; charset=utf-8")
    log(f"pointer {name} -> {version}")


# ----------------------------------------------------------------- prune --

def bucket_versions():
    """列出 bucket 里已完整同步(manifest 已上传)的版本."""
    versions = set()
    paginator = s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{BASE_PATH}/", Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            v = cp["Prefix"].rstrip("/").rsplit("/", 1)[-1]
            if VERSION_RE.match(v) and object_size(key(v, "manifest.json")) is not None:
                versions.add(v)
    return versions


def vkey(v):
    nums, _, suffix = v.partition("-")
    return (tuple(int(x) for x in nums.split(".")), suffix)


def prune(retention, keep_extra, dry_run):
    versions = bucket_versions()
    keep = set(sorted(versions, key=vkey, reverse=True)[:retention]) | set(keep_extra)
    for v in sorted(versions - keep, key=vkey):
        log(f"pruning old version {v}")
        if dry_run:
            continue
        paginator = s3().get_paginator("list_objects_v2")
        objs = [o["Key"]
                for page in paginator.paginate(Bucket=BUCKET, Prefix=key(v) + "/")
                for o in page.get("Contents", [])]
        for i in range(0, len(objs), 1000):
            s3().delete_objects(Bucket=BUCKET, Delete={
                "Objects": [{"Key": k} for k in objs[i:i + 1000]], "Quiet": True})


# ------------------------------------------------------------------ misc --

def upload_install_scripts(dry_run):
    """把同目录下的安装脚本传到 bucket 根, 供 irm/curl 一键安装."""
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("install.ps1", "install.sh"):
        path = os.path.join(here, name)
        if not os.path.isfile(path):
            continue
        if dry_run:
            log(f"would upload {name}")
            continue
        with open(path, "rb") as f:
            s3().put_object(Bucket=BUCKET, Key=name, Body=f,
                            ContentType="text/plain; charset=utf-8",
                            CacheControl=SHORT_CACHE)
        log(f"uploaded {name}")


def upload_status(latest, stable, failures, dry_run):
    status = {
        "lastSync": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "latest": latest,
        "stable": stable,
        "versions": sorted(bucket_versions(), key=vkey, reverse=True),
        "failures": failures,
    }
    if dry_run:
        return
    put_text(key("mirror-status.json"), json.dumps(status, indent=2), "application/json")
    log("mirror-status.json updated")


# ------------------------------------------------------------------ main --

def main():
    ap = argparse.ArgumentParser(description="Sync Claude Code releases into RustFS")
    ap.add_argument("--retention", type=int,
                    default=int(os.environ.get("MIRROR_RETENTION", "8")),
                    help="保留最近 N 个版本 (默认 8)")
    ap.add_argument("--public", action="store_true",
                    help="给 bucket 设置匿名只读策略")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not ACCESS_KEY or not SECRET_KEY:
        die("请先设置 RUSTFS_ACCESS_KEY / RUSTFS_SECRET_KEY 环境变量")

    ensure_bucket(args.public and not args.dry_run)

    try:
        latest = upstream_pointer("latest")
        stable = upstream_pointer("stable")
    except Exception as e:
        die(f"无法获取上游版本信息: {e}")
    log(f"upstream: latest={latest} stable={stable}")

    ok, failures = set(), []
    for v in dict.fromkeys([latest, stable]):  # 去重且保持顺序
        try:
            if sync_version(v, args.dry_run):
                ok.add(v)
                if v == latest:
                    upload_pointer("latest", v, args.dry_run)
                if v == stable:
                    upload_pointer("stable", v, args.dry_run)
            else:
                failures.append(v)
        except Exception as e:
            failures.append(v)
            log(f"{v}: ERROR {e}")

    prune(args.retention, {latest, stable}, args.dry_run)
    upload_install_scripts(args.dry_run)
    upload_status(latest, stable, failures, args.dry_run)

    if failures:
        die(f"sync finished with failures: {failures}")
    log("sync finished")


if __name__ == "__main__":
    main()
