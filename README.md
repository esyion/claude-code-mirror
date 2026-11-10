# Claude Code 镜像站

定时把 `https://downloads.claude.ai/claude-code-releases` 的全部 8 个平台二进制
（darwin-x64/arm64、linux-x64/arm64(+musl)、win32-x64/arm64，每个约 290MB）
镜像到自有存储，对外保持与官方**完全一致的 URL 结构**，安装脚本只需换域名。

两种部署方案，二选一：

| | 方案 B：CF + GitHub（推荐，零服务器） | 方案 A：自建 RustFS |
|---|---|---|
| 定时拉取 | GitHub Actions cron（免费） | 服务器 cron + sync.py |
| 存储 | GitHub Releases | RustFS (S3) |
| 分发域名 | CF Worker（流式代理） | Cloudflare 反代 RustFS |
| 需要服务器 | ❌ | ✅ |

---

## 方案 B：Cloudflare Worker + GitHub（零服务器）

```
                ┌─ 定时(每6h) ─────────────────────────┐
                │  GitHub Actions (gh_sync.py)          │
                │  拉取官方 8 平台 → SHA256 校验         │
                │  → 存为 GitHub Releases               │
                │  → 更新 channels/latest|stable        │
                └──────────────┬────────────────────────┘
                               │ 存储
                               ▼
  客户端 ──irm──▶ CF Worker (你的域名) ──流式代理──▶ GitHub Releases / raw
                 /install.ps1            ← 仓库 raw
                 /claude-code-releases/latest|stable ← 仓库 channels/
                 /claude-code-releases/<ver>/<plat>/claude ← Release 资产
```

> 为什么不让 CF Worker 直接搬运？Worker 单实例 128MB 内存上限，且 GitHub 资产
> 上传接口对流式无 Content-Length 的请求支持差；Actions 拉取+上传是原生能力，
> Worker 只做流式转发内存恒定，各干各擅长的事。

### 1. 建仓库

本目录整体 push 到一个 **public** 仓库（必须 public：私有仓库的 Release 资产
下载要鉴权，Worker 无法匿名代理）。目录结构：

```
├── .github/workflows/sync.yml   # 定时任务
├── gh_sync.py                   # 同步逻辑 (拉取/校验/上传/清理)
├── channels/latest|stable       # 版本指针, Actions 自动维护
├── install.ps1 / install.sh     # 安装脚本 ($MIRROR 改成你的 Worker 域名)
└── worker/                      # CF Worker
```

### 2. 触发首次同步

仓库 **Actions → mirror-sync → Run workflow** 手动跑一次（约 3~5 分钟，
下载+上传 2.3GB）。跑完确认 Releases 页出现 8 个平台资产 + manifest.json，
`channels/` 下生成了指针文件。

### 3. 部署 Worker

```bash
cd worker
npm i -g wrangler && wrangler login
# 改 wrangler.toml: GH_REPO = "你的用户名/仓库名"
wrangler deploy
```

默认得到 `https://claude-code-mirror.<子域>.workers.dev`；要用自己的
Cloudflare 域名，取消 `wrangler.toml` 里 `routes` 的注释。

### 4. 使用

把 `install.ps1` 的 `$MIRROR`（和 `install.sh` 的 `MIRROR`）改成 Worker 域名，
提交 push 即可——Worker 从仓库 raw 读脚本，自动就是最新的。

```powershell
irm https://mirror.example.com/install.ps1 | iex
```

```bash
curl -fsSL https://mirror.example.com/install.sh | bash
```

验证：`curl -s https://mirror.example.com/claude-code-releases/latest`。

### 注意事项

- **GitHub 限制**：Release 单文件上限 2GiB（Claude 二进制 ~290MB ✓）；无总配额
  硬限制，但保留策略（默认 8 版 ≈ 18GB）别开太大。Public 仓库 Actions 免费。
- **定时精度**：Actions cron 有排队延迟（几分钟到偶尔更久），镜像场景无所谓；
  另外 GitHub 会在仓库 60 天无提交后停用定时任务——本仓库每次版本更新都会
  自动提交 channels，基本不会触发，长期没发版时手动跑一次即可。
- **国内访问**：客户端只访问你的 CF 域名；GitHub/raw 的拉取都发生在 CF 边缘。
- **二进制不做边缘缓存**：GitHub 下载会 302 到一次性签名 URL，边缘缓存不可靠，
  Worker 直通转发（响应头带 immutable，客户端不会重复下载）。如需边缘缓存，
  可给 Worker 加 R2（免费 10GB，约够 3 个版本），当前未做。

---

## 方案 A：自建 RustFS + sync.py

见 `sync.py`（同步工具）与下文。适合已有服务器/RustFS 的情况。

### 镜像后的 bucket 布局

```
s3://claude-mirror/
├── install.ps1 / install.sh                    ← 安装入口 (sync.py 自动上传)
└── claude-code-releases/
    ├── latest | stable                         ← 纯文本版本号 (text/plain, 短缓存)
    ├── mirror-status.json                      ← 最近同步状态, 排障用
    └── {version}/
        ├── manifest.json                       ← 官方原样 manifest (含 SHA256)
        └── <platform>/claude(.exe)             ← 8 个平台
```

安全性：同步时按官方 manifest 的 SHA256 逐一校验后才入库；客户端安装时再次校验。
manifest 在所有二进制上传完成后才写入，`manifest.json` 存在即代表该版本完整。

### 1. 部署同步

```bash
pip install boto3 requests

export RUSTFS_ENDPOINT="http://127.0.0.1:9000"   # RustFS 内网地址, 不要走 Cloudflare
export RUSTFS_ACCESS_KEY="xxx"
export RUSTFS_SECRET_KEY="xxx"
export RUSTFS_BUCKET="claude-mirror"             # 可选, 默认 claude-mirror

# 首次运行: 建桶 + 设置匿名只读策略 + 全量同步 (约 2.3GB 下载)
python sync.py --public
```

其余可选项：`MIRROR_BASE_PATH`（对象前缀）、`MIRROR_WORKERS`（并发，默认 4）、
`MIRROR_RETENTION`（保留版本数，默认 8）。

cron（RUSTFS 所在服务器）：

```cron
17 */6 * * * RUSTFS_ENDPOINT=http://127.0.0.1:9000 RUSTFS_ACCESS_KEY=xxx RUSTFS_SECRET_KEY=xxx \
    /usr/bin/python3 /opt/claude-mirror/sync.py >> /var/log/claude-mirror.log 2>&1
```

### 2. 对外暴露 (Cloudflare)

1. RustFS 对外提供 HTTPS 入口（nginx/caddy 反代或 RustFS 自身 TLS），Cloudflare
   DNS 代理到该入口，域名如 `mirror.example.com`。
2. bucket 匿名只读策略（`sync.py --public` 已自动完成）：
   ```json
   {"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":"*",
     "Action":["s3:GetObject"],"Resource":["arn:aws:s3:::claude-mirror/*"]}]}
   ```
3. 缓存：版本化对象已带 `immutable` 头，CF 直接边缘缓存；`latest`/`stable`/
   安装脚本已带 `max-age=300`。

### 3. 使用

`install.ps1` 里 `$MIRROR`（`install.sh` 里 `MIRROR`）改成你的域名，跑一次
`python sync.py` 推送脚本到 bucket 根。

```powershell
irm https://mirror.example.com/install.ps1 | iex
```

```bash
curl -fsSL https://mirror.example.com/install.sh | bash
```

### 注意事项

- **体积**：每版本 8 平台 ≈ 2.3GB；默认保留 8 版 ≈ 18GB，`--retention N` 可调。
- **同步走内网**：`RUSTFS_ENDPOINT` 必须是 RustFS 直连地址，300MB 大对象过
  Cloudflare 上传会超时；CF 只做下行分发。
- **CF 免费版单文件缓存上限 512MB**，~290MB 的二进制没有问题。
- **故障退出码**：同步失败 `sync.py` 以非零退出，cron/监控可直接告警。

---

## 通用说明

- Claude 程序内的自动更新走 `downloads.claude.ai` 原域名；镜像覆盖的是安装/重装
  场景。如需劫持更新，可配合 hosts 或反代官方域名（自担风险）。
- 两种方案的安装脚本完全相同，只是 `$MIRROR` 指向不同；URL 布局与官方一致，
  以后想从方案 B 切回方案 A（或反过来），只改域名即可。
