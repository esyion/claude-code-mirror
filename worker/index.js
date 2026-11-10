// ============================================================
// Claude Code 镜像 Worker
// 把 GitHub Releases 伪装成 downloads.claude.ai 的 URL 结构,
// 客户端只需要把安装脚本里的 $MIRROR 指向本 Worker 的域名.
//
//   /install.ps1 | /install.sh                  -> 仓库 raw 文件
//   /claude-code-releases/latest|stable         -> 仓库 channels/ 指针
//   /claude-code-releases/<ver>/manifest.json   -> Release 资产
//   /claude-code-releases/<ver>/<plat>/claude*  -> Release 资产
//
// 部署: cd worker && wrangler deploy   (先改 wrangler.toml 里的 GH_REPO)
// ============================================================

const CHANNELS_TTL = 60;       // latest/stable 指针, 秒
const SCRIPTS_TTL = 300;       // 安装脚本
const MANIFEST_TTL = 86400;    // 版本化 manifest, 一天 (raw/资产不可变, 缓存只是保险)

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("method not allowed\n", { status: 405 });
    }

    const { pathname: path } = new URL(request.url);
    const repo = env.GH_REPO;
    if (!repo) {
      return new Response("GH_REPO env not configured\n", { status: 500 });
    }
    const branch = env.BRANCH || "main";

    const raw = (file) => `https://raw.githubusercontent.com/${repo}/${branch}/${file}`;
    const asset = (tag, name) => `https://github.com/${repo}/releases/download/${tag}/${name}`;

    if (path === "/") {
      return new Response(
        "Claude Code mirror\nUsage: irm https://<this-domain>/install.ps1 | iex\n",
        { headers: { "content-type": "text/plain; charset=utf-8" } },
      );
    }

    // 安装脚本 -> 仓库 raw
    if (path === "/install.ps1" || path === "/install.sh") {
      return proxySmall(raw(path.slice(1)), request, ctx, SCRIPTS_TTL);
    }

    // 版本指针 -> channels/
    let m = path.match(/^\/claude-code-releases\/(latest|stable)\/?$/);
    if (m) return proxySmall(raw(`channels/${m[1]}`), request, ctx, CHANNELS_TTL);

    // manifest.json -> Release 资产
    m = path.match(/^\/claude-code-releases\/(\d+\.\d+\.\d+(?:-[\w.+-]+)?)\/manifest\.json$/);
    if (m) return proxySmall(asset(m[1], "manifest.json"), request, ctx, MANIFEST_TTL);

    // 二进制 -> Release 资产, 流式转发 (不缓存: GitHub 下载是 302 到一次性签名 URL,
    // 边缘缓存命中率不可靠; 290MB 也避免 Cache API 的 clone 内存压力)
    m = path.match(/^\/claude-code-releases\/(\d+\.\d+\.\d+(?:-[\w.+-]+)?)\/([\w-]+)\/(claude(?:\.exe)?)\/?$/);
    if (m) {
      const upstream = await fetch(asset(m[1], `${m[2]}.${m[3]}`), { redirect: "follow" });
      const headers = new Headers(upstream.headers);
      headers.set("Cache-Control", "public, max-age=31536000, immutable");
      return new Response(upstream.body, { status: upstream.status, headers });
    }

    return new Response("not found\n", { status: 404 });
  },
};

// 小文件 (KB 级): 用 Cache API 做边缘缓存
async function proxySmall(upstream, request, ctx, ttl) {
  const cache = caches.default;
  if (request.method === "GET") {
    const hit = await cache.match(request);
    if (hit) return hit;
  }

  const resp = await fetch(upstream, { redirect: "follow" });
  if (!resp.ok) {
    return new Response(`upstream ${resp.status}\n`, {
      status: resp.status,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  }

  const headers = new Headers(resp.headers);
  headers.set("Cache-Control", `public, max-age=${ttl}`);
  const out = new Response(resp.body, { status: resp.status, headers });
  if (request.method === "GET" && ctx) {
    ctx.waitUntil(cache.put(request, out.clone()));
  }
  return out;
}
