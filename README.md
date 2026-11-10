# Claude Code 镜像站

零服务器方案：**GitHub Actions 定时同步官方发布 → GitHub Releases 存储 →
Cloudflare Worker 提供自有域名**。对外 URL 结构与官方
`downloads.claude.ai/claude-code-releases` 完全一致，**每个版本一个 Release**，
支持逐版本同步与历史补齐。

## 已部署实例

- 安装（Windows）：`irm https://claude-code-mirror.qingbo-my.workers.dev/install.ps1 | iex`
- 安装（macOS/Linux）：`curl -fsSL https://claude-code-mirror.qingbo-my.workers.dev/install.sh | bash`
- Worker：<https://claude-code-mirror.qingbo-my.workers.dev>
- 存储仓库：本仓库（[esyion/claude-code-mirror](https://github.com/esyion/claude-code-mirror)）的 Releases

## 架构

```
              ┌─ 定时(每6h) ─────────────────────────────┐
              │  GitHub Actions (gh_sync.py)             │
              │  拉取官方 8 平台二进制 → SHA256 校验       │
              │  → 每版本一个 GitHub Release              │
              │  → 补齐缺失的中间版本 (BACKFILL)          │
              │  → 更新 channels/latest|stable           │
              │  → 按保留策略清理 (RETENTION)             │
              └──────────────┬───────────────────────────┘
                             │ 存储
                             ▼
客户端 ─irm/curl─▶ CF Worker ──流式代理──▶ GitHub Releases / raw
```

> 为什么不让 Worker 直接搬运二进制？Worker 单实例 128MB 内存上限，且 GitHub 资产
> 上传接口对流式无 Content-Length 的请求支持差；Actions 拉取+上传是原生能力，
> Worker 只做流式转发内存恒定，各干各擅长的事。

## 仓库结构

```
├── .github/workflows/sync.yml   # 定时任务 (schedule + 手动触发)
├── gh_sync.py                   # 同步逻辑
├── channels/latest|stable       # 版本指针, Actions 自动提交 (Worker 读取它们)
├── install.ps1 / install.sh     # 安装脚本, 从 raw 分发, 改完 push 即生效
└── worker/                      # CF Worker (wrangler deploy)
```

git 仓库本体始终 <1MB——290MB 的二进制走 Release 资产，不进 git 历史、不用 LFS。

## 同步机制

每次运行（`gh_sync.py`）：

1. 拉取官方 `latest`/`stable` 指针，同步这两个版本
2. **逐版本补齐**：从 npm registry（与官方原生构建同版本号）枚举历史版本，
   找出本仓库 Release 里还没有的，从新到旧补 `BACKFILL` 个（默认 4）
3. 每个版本：下载 8 平台二进制 → 按官方 manifest SHA256 逐一校验 →
   建 Release（tag=版本号）上传 9 个资产（8 平台 + manifest.json）
4. 提交 `channels/latest`、`channels/stable` 指针（有变化才提交）
5. 按 `RETENTION` 清理旧 Release（始终保留 latest/stable 所指版本）

两个行为细节：

- **断点续跑**：以 Release 资产是否齐全判断版本完成度，中途失败/超时后再次
  触发会接着跑，不会重复下载已完成的版本
- **npm-only 版本自动跳过**：少数版本只有 npm 包没有原生构建，probe 404 即跳过
- `RETENTION>0` 时只补齐保留窗口内（比最旧保留版更新）的版本，避免"同步完就
  被清理"的空转；`RETENTION=0` 时全量补齐

### 环境变量（.github/workflows/sync.yml）

| 变量 | 默认 | 说明 |
|---|---|---|
| `RETENTION` | `8` | 保留最近 N 个版本；`0` = 永久保留全部 |
| `BACKFILL` | `4` | 每次运行最多补齐的缺失版本数 |

### 体积参考

每版本 8 平台 × ~290MB ≈ **2.3GB**。保留 8 版 ≈ 18GB；全量历史（~350 个版本）
≈ **800GB**，请确认这是你想要的再改 `RETENTION=0`。

## 常用操作

```bash
# 查看同步历史 / 状态
gh run list --workflow=mirror-sync --limit 5

# 手动触发一次常规同步
gh workflow run sync.yml

# 手动触发并补齐最多 999 个缺失版本 (全量历史补齐; 5 小时超时后接着再点即可, 会续跑)
gh workflow run sync.yml -f backfill=999

# 更新 Worker 代码 (改了 worker/index.js 或 wrangler.toml 后)
cd worker && wrangler deploy
```

全量历史补齐的前置步骤：先把 workflow 里 `RETENTION` 改成 `"0"` 并提交，
否则补齐的旧版本会在当次运行末尾被清理掉。

### 绑定自己的 Cloudflare 域名

1. `worker/wrangler.toml` 取消 `routes` 注释，填入你的域名（该域名需在
   Cloudflare 托管 DNS），`cd worker && wrangler deploy`
2. 把 `install.ps1` 的 `$MIRROR`（和 `install.sh` 的 `MIRROR`）换成新域名，push

## Worker 路由

| 路径 | 来源 |
|---|---|
| `/install.ps1`、`/install.sh` | 仓库 raw（缓存 5 分钟，push 即渐近生效） |
| `/claude-code-releases/latest`、`/stable` | 仓库 `channels/`（缓存 60s） |
| `/claude-code-releases/<ver>/manifest.json` | Release 资产（缓存 1 天） |
| `/claude-code-releases/<ver>/<platform>/claude(.exe)` | Release 资产，流式转发 |

二进制不做边缘缓存：GitHub 下载会 302 到一次性签名 URL，缓存命中率不可靠，
290MB 大对象走 Cache API 也有 clone 内存压力；直通转发 + 响应头
`immutable`，客户端不会重复下载。

## 注意事项

- 仓库必须 **public**：私有仓库的 Release 资产下载要鉴权，Worker 无法匿名代理
- GitHub 会在仓库 60 天无提交后停用定时任务——每次版本更新都会自动提交
  `channels/`，基本不会触发；长期没发版时手动跑一次即可
- Actions cron 有排队延迟（几分钟到偶尔更久），镜像场景无所谓
- Claude 程序内的自动更新仍走 `downloads.claude.ai` 原域名，本镜像覆盖
  安装/重装场景；如需劫持更新可配合 hosts 或反代官方域名（自担风险）
