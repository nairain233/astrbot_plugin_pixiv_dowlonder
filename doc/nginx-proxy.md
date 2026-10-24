# 使用 Nginx 代理 Pixiv 作品信息与图片

本文以 Debian／Ubuntu 上的一台境外服务器为例：一个 Nginx、一个 HTTPS 域名，通过两个路径前缀分别代理作品信息和图片，并只允许 AstrBot 的公网出口 IP 访问。也可以只启用作品信息反代，图片继续使用 Worker 或直连。

**如果域名接入 Cloudflare，推荐开启橙云，使用「Cloudflare 橙云代理 + Cloudflare WAF IP 白名单 + 源站防火墙」**：WAF 按 AstrBot 的真实出口 IP 限制访问，源站防火墙只允许 Cloudflare 回源。无需在 Nginx 中依赖 `X-Forwarded-For` 恢复客户端 IP 来做访问控制，具体配置见[第 8 节](#8-cloudflare-橙云部署推荐)。

本教程用于公网 HTTPS，由 Nginx 直接转发 Pixiv，不需要另设 FastAPI。若通过 Tailscale IP 和端口访问，使用 [Tailscale + FastAPI 内网教程](tailscale.md)，直接运行绑定 Tailscale 地址的 FastAPI HTTP 服务，无需 Nginx。两套方案沿用相同的 `/pixiv`、`/pximg` 插件路径。

## 1. 为什么没有继续用一个 Worker 代理全部请求

插件需要访问两个不同的上游：

| 阶段 | 上游 | 返回内容 |
| --- | --- | --- |
| 作品解析 | `www.pixiv.net` | 作品详情、年龄分级和分页图片链接等 JSON |
| 图片下载 | `i.pximg.net` | 所选尺寸的图片文件 |

此前对配置的 Worker 域名进行排查时，同一作品的详情接口经 Worker 返回 **HTTP 403 和非 JSON 页面**，直连官方接口则能返回 JSON；图片仍可经 Worker 下载。这说明图片链路通畅，但作品信息链路不通畅。

因此，README 保留的精简 Worker 示例只负责图片，插件将两阶段的反代独立配置。你可以选择“自建 Nginx 获取信息 + Worker 下载图片”，也可以像本文这样让一个 Nginx 承担两阶段。**Nginx 同样不保证绕过 Pixiv 的访问限制**：服务器出口仍须能够取得作品 JSON，账号也须具有作品查看权限。

## 2. 准备域名、出口 IP 和 HTTPS

准备以下内容，示例值需要替换：

| 示例值 | 替换为 |
| --- | --- |
| `pixiv-proxy.example.com` | 你的反代域名，DNS 指向境外服务器 |
| `203.0.113.10` | 运行 AstrBot 的网络访问这台服务器时使用的公网出口 IPv4 |
| `2001:db8::10` | AstrBot 的公网出口 IPv6；不使用时不添加对应规则 |
| 证书路径 | 覆盖该域名且受 AstrBot 系统信任的证书和私钥 |

### 白名单配置说明

白名单中填写的是 **AstrBot 的公网出口 IP**。

- **橙云模式**：在 Cloudflare WAF 中放行 AstrBot 的出口 IP，在源站防火墙中放行 Cloudflare 的回源 IP 网段。
- **灰云模式**：在 Nginx 白名单和源站防火墙中放行 AstrBot 的出口 IP。

如果 AstrBot 通过 NAT、VPN 等方式访问服务器，应以服务端实际看到的来源 IP 为准。IPv4 和 IPv6 需要分别配置；出口 IP 发生变化时，也要及时更新白名单。

### Cloudflare 代理模式选择

如果 DNS 托管在 Cloudflare，**推荐使用橙云（已代理）**，并按照第 8 节配置 WAF 白名单和源站防火墙。同时，删除覆盖该域名相关路径的 Worker 路由，确保请求能够正常回源到 Nginx。

只有需要客户端直接连接 Nginx 时，才使用灰云（仅 DNS），并配置下方的直连版 Nginx 白名单。

详细说明：[Cloudflare 代理状态](https://developers.cloudflare.com/dns/proxy-status/)。

**注意：** 如果源站只配置了 IPv4，不要保留指向该源站的错误 AAAA 记录。


在境外服务器安装工具：

```bash
sudo apt update
sudo apt install nginx ca-certificates curl certbot
```

已有受信任证书可直接使用；没有证书时，可用 DNS-01 手动签发，不要求向所有人开放 HTTP 80：

```bash
sudo certbot certonly --manual --preferred-challenges dns -d "pixiv-proxy.example.com"
```

按提示添加 `_acme-challenge.pixiv-proxy.example.com` 的 TXT 记录，等待 DNS 生效后继续。证书通常位于下方配置中的 `/etc/letsencrypt/live/域名/`，以 Certbot 实际输出为准。**手动 DNS 验证且未配置自动认证钩子时不能自动续期**；到期前须重复签发命令并重新加载 Nginx，长期使用可改为 DNS 提供商的自动化插件。参见 [Certbot 手动签发及续期说明](https://eff-certbot.readthedocs.io/en/stable/using.html#manual)。直连 Nginx 的客户端需要受信任证书，不能直接把仅供 CDN 回源使用的证书当作公开可信证书。

橙云模式下，在服务器安全组／防火墙中仅允许 Cloudflare 官方回源网段访问 TCP 443；直连模式则仅允许 AstrBot 出口访问 TCP 443。保留现有 SSH 管理规则。本例没有 HTTP 服务，也不需要开放 80；用上述 DNS-01 方式验证域名不受 HTTP 来源 IP 白名单影响。

## 3. 先验证境外服务器能获取作品信息

在境外服务器执行，`12345678` 只是格式示例，替换为一个已知公开可访问的静态作品 PID：

```bash
curl -sS -D - \
  -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0" \
  -H "Referer: https://www.pixiv.net/artworks/12345678" \
  -H "Accept: application/json" \
  -H "Accept-Language: zh-CN,zh;q=0.8,zh-TW;q=0.2" \
  "https://www.pixiv.net/ajax/illust/12345678"
```

预期 HTTP 200、`Content-Type: application/json`，且响应 `error` 为 `false`、`body` 是作品详情。如果这里已是 403 HTML，先排查服务器出口、作品权限和上游访问限制；加一个 Nginx 不会自动修好这条上游链路。此命令先测匿名公开作品，不需要发送 Cookie；必须登录的作品可在基础链路确认后通过插件配置验证。

## 4. 一个 Nginx 同时代理信息、图片并设置白名单

创建 `/etc/nginx/conf.d/pixiv-proxy.conf`，放入以下完整配置。它应被 `/etc/nginx/nginx.conf` 的 `http {}` 中的 `include /etc/nginx/conf.d/*.conf;` 加载；常见 Debian／Ubuntu 包已包含该设置。如使用面板，需要把 `log_format` 放在 `http {}` 中，不能放进站点的 `server {}`。

**下方 `allow`／`deny` 块用于直连模式。开启橙云时，必须按第 8 节移除该块，并先落实 WAF 和源站防火墙规则**；否则 Nginx 看到 Cloudflare 节点地址，会把合法请求也拒绝。

不要覆盖已有同域名的站点配置；已有站点应合并对应配置，避免同一监听端口重复定义该域名。

```nginx
log_format pixiv_upstream '$remote_addr [$time_local] "$request_method $uri" '
                          'status=$status upstream_status=$upstream_status '
                          'upstream=$upstream_addr type=$upstream_http_content_type '
                          'request_time=$request_time upstream_time=$upstream_response_time';

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name pixiv-proxy.example.com;

    ssl_certificate /etc/letsencrypt/live/pixiv-proxy.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pixiv-proxy.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    # Allow the actual public egress addresses of the AstrBot host.
    allow 203.0.113.10;
    # allow 2001:db8::10;
    deny all;

    access_log /var/log/nginx/pixiv-proxy.access.log pixiv_upstream;
    error_log /var/log/nginx/pixiv-proxy.error.log warn;

    # The plugin only reads upstream resources.
    if ($request_method !~ ^(GET|HEAD)$) {
        return 405;
    }

    proxy_http_version 1.1;
    proxy_ssl_server_name on;
    proxy_ssl_verify on;
    proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
    proxy_ssl_verify_depth 3;
    proxy_connect_timeout 10s;
    proxy_read_timeout 60s;
    proxy_send_timeout 60s;
    proxy_next_upstream off;
    proxy_cache off;
    proxy_buffering off;
    proxy_intercept_errors off;
    proxy_redirect off;
    proxy_hide_header Set-Cookie;

    # Preserve the official AJAX path and its query string.
    location /pixiv/ajax/illust/ {
        proxy_pass https://www.pixiv.net/ajax/illust/;
        proxy_ssl_name www.pixiv.net;
        proxy_set_header Host www.pixiv.net;
        proxy_set_header Connection "";
        proxy_set_header Referer $http_referer;
        proxy_set_header Cookie $http_cookie;
        proxy_set_header Authorization "";
    }

    # Remove only the local image prefix; never send login cookies here.
    location /pximg/ {
        proxy_pass https://i.pximg.net/;
        proxy_ssl_name i.pximg.net;
        proxy_set_header Host i.pximg.net;
        proxy_set_header Connection "";
        proxy_set_header Referer https://www.pixiv.net/;
        proxy_set_header Cookie "";
        proxy_set_header Authorization "";
    }

    location / {
        return 404;
    }
}
```

所有 `allow` 都放在 `deny all` 前面，可以增加多个固定地址或自己控制的 CIDR；不需要 IPv6 监听且系统未启用 IPv6 时，去掉 `listen [::]:443 ssl;`。两个代理 `location` 继承 `server` 的白名单，不要单独添加 `allow all`。规则按顺序首次匹配，继承规则见 [Nginx access 模块](https://nginx.org/en/docs/http/ngx_http_access_module.html)。防火墙先拒绝连接时，请求不会出现在 Nginx 日志中。

映射结果如下，查询参数原样传递：

| 客户端请求 | Nginx 请求的上游 |
| --- | --- |
| `/pixiv/ajax/illust/12345678` | `https://www.pixiv.net/ajax/illust/12345678` |
| `/pixiv/ajax/illust/12345678/pages?lang=zh` | `https://www.pixiv.net/ajax/illust/12345678/pages?lang=zh` |
| `/pximg/img-original/img/.../12345678_p0.jpg` | `https://i.pximg.net/img-original/img/.../12345678_p0.jpg` |
| `/pximg/c/540x540_70/img-master/img/.../12345678_p0_master1200.jpg` | `https://i.pximg.net/c/540x540_70/img-master/img/.../12345678_p0_master1200.jpg` |

保留 `location` 和 `proxy_pass` 中的末尾斜杠：这里依靠 Nginx 的 URI 前缀替换规则去掉本地 `/pixiv` 或 `/pximg`，没有额外 `rewrite`。HTTPS 上游同时指定 Host、SNI 并校验证书；图片路径显式清空 Cookie，作品路径才转发插件提供的 Cookie。未设置的普通请求头（如插件的 User-Agent、Accept-Language）继续传递。相关指令见 [Nginx proxy 模块](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)。

本配置不缓存作品或图片，不改写作品 JSON 中的原始图片域名。插件收到 `i.pximg.net` 链接后，会在图片下载阶段按自己的配置替换域名。`proxy_redirect off` 只关闭响应重定向地址的改写，不会把 3xx 变为成功；上游返回 3xx 时插件仍拒绝跟随。

检查配置成功后再启动或重新加载：

```bash
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

若 `nginx -t` 失败，先修复错误，不执行后续命令。常见原因是证书路径错误、域名解析失败，或 `log_format` 被放在 `server {}` 内。

## 5. 插件中填写什么

### 信息和图片都走这台 Nginx

```json
{
  "artwork_proxy_base_url": "pixiv-proxy.example.com/pixiv",
  "image_proxy_base_url": "pixiv-proxy.example.com/pximg"
}
```

省略协议默认 HTTPS。两项使用同一个域名，但本例必须分别带 `/pixiv` 和 `/pximg`，不能都只填域名根路径。这里展示的是需修改的配置项，不要用它覆盖整个配置文件。

```text
作品信息：AstrBot → Cloudflare（橙云 + WAF）→ Nginx /pixiv/ajax/illust/... → www.pixiv.net
图片下载：AstrBot → Cloudflare（橙云 + WAF）→ Nginx /pximg/图片路径         → i.pximg.net
```

上图为推荐的橙云链路；直连模式省去 Cloudflare 这一跳。运行 AstrBot 的机器只需能访问这个反代域名；境外 Nginx 服务器负责访问两个 Pixiv 上游。需要登录时，把 Cookie 填在插件的 `pixiv_cookie` 中，不要写进 Nginx 配置。Cookie 会经 Cloudflare（橙云模式）和你的 Nginx 发给作品接口，图片请求不携带 Cookie；因此代理链路须可信。健康模式仍会依据作品详情的分级标记决定是否继续下载。

### 只用 Nginx 获取作品信息

使用同一配置即可；如果不需要提供图片服务，也可以删除整个 `/pximg/` 的 `location` 块，其余配置保留。

```json
{
  "artwork_proxy_base_url": "pixiv-proxy.example.com/pixiv",
  "image_proxy_base_url": "images.example.com"
}
```

其中 `images.example.com` 是你已部署的图片 Worker 或其他图片反代；若图片下载可以直连，则把 `image_proxy_base_url` 留空。两个阶段互不串用地址，失败时不自动回退直连。保存配置并重载插件后验证。

## 6. 验证完整链路和白名单

以下命令在 **运行 AstrBot 的机器／相同网络出口** 执行。示例使用 Bash；Windows PowerShell 可将 `curl` 换为 `curl.exe` 并将命令写成一行。先使用公开作品，不要把 Cookie 填入这些公开示例。

```bash
curl -sS -D - \
  -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0" \
  -H "Referer: https://www.pixiv.net/artworks/12345678" \
  -H "Accept: application/json" \
  -H "Accept-Language: zh-CN,zh;q=0.8,zh-TW;q=0.2" \
  "https://pixiv-proxy.example.com/pixiv/ajax/illust/12345678"

curl -sS -D - \
  -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0" \
  -H "Referer: https://www.pixiv.net/artworks/12345678" \
  -H "Accept: application/json" \
  -H "Accept-Language: zh-CN,zh;q=0.8,zh-TW;q=0.2" \
  "https://pixiv-proxy.example.com/pixiv/ajax/illust/12345678/pages?lang=zh"
```

预期两次均为 HTTP 200、JSON 且 `error=false`。从分页响应中复制一条实际存在的 `urls.original`：例如它以 `https://i.pximg.net/` 开头，就将这一段替换成 `https://pixiv-proxy.example.com/pximg/`，路径和查询参数保持不变。然后进行一次 GET，将图片正文丢弃，只检查响应头：

```bash
curl -sS -D - -o /dev/null "https://pixiv-proxy.example.com/pximg/实际图片路径"
```

预期 HTTP 200 且 `Content-Type` 为 `image/...`。不要直接使用教程中的 `...` 或“实际图片路径”。这些命令没有 `-L`，与插件一样不跟随重定向。

再从一个**未加入白名单**的网络访问同一个有效作品接口：橙云模式预期由 Cloudflare WAF 拦截，可在 Cloudflare 安全事件中确认，请求不应到达源站；直连模式预期 Nginx 返回 403，或被源站防火墙拒绝／超时。橙云模式还须从公网绕过 Cloudflare，使用源站 IP 并保持原域名的 Host／TLS SNI 测试，预期连接被防火墙拒绝或超时。测试白名单时不要只访问 `/`，该路径在本配置中固定返回 404。

最后在 QQ 发送 `/pixiv 实际PID`，使用一个公开的全年龄静态作品验证插件完整下载。代理接口通畅但作品被健康模式拦截时，不应出现分页和图片请求，这是正常行为。

## 7. 用上游日志定位失败阶段

在境外服务器查看：

```bash
sudo tail -f "/var/log/nginx/pixiv-proxy.access.log" "/var/log/nginx/pixiv-proxy.error.log"
```

访问日志包含连接来源、路径、本机状态码、上游状态码、上游地址、响应类型和耗时。直连模式来源是 AstrBot 出口；橙云模式来源是 Cloudflare 节点，客户端 IP 和 WAF 拦截情况在 Cloudflare 安全事件中检查。例如，直连模式下：

```text
203.0.113.10 [...] "GET /pixiv/ajax/illust/12345678" status=403 upstream_status=- upstream=- type=- ...
203.0.113.10 [...] "GET /pixiv/ajax/illust/12345678" status=403 upstream_status=403 upstream=... type=text/html ...
203.0.113.10 [...] "GET /pximg/img-original/..." status=200 upstream_status=200 upstream=... type=image/jpeg ...
```

| 现象 | 排查方向 |
| --- | --- |
| 本机 403，`upstream_status=-` | 请求未到上游；检查来源 IP 白名单，以及 error 日志中的 `access forbidden by rule` |
| 本机 403，`upstream_status=403` | 上游确实返回 403；作品接口检查 Pixiv 出口、权限和登录状态，不能靠放宽白名单修复 |
| `upstream_status=200` 但作品响应为 HTML | 作品 JSON 链路未通过，检查上游验证页；200 本身不代表解析成功 |
| 502 | 查看 error 日志中的 DNS、连接和证书／SNI 错误；检查 `ca-certificates` 和服务器时间 |
| 504／插件超时 | 上游读取超时或出口不通；插件默认单请求总超时 60 秒，与 Nginx 的读取间隔超时不是同一个计时规则 |
| 301／302 | 本机站点规则或上游发生跳转；插件不会跟随，不能依赖跳回官方域名完成访问 |
| 404 且没有上游状态 | 检查插件两项地址是否带正确路径前缀，是否命中了预期站点；访问根路径 404 是正常的 |
| 看不到任何请求日志 | 橙云模式先检查 Cloudflare 安全事件中的拦截记录，再检查 DNS、端口、安全组、TLS 握手和是否误走 Worker |

自定义访问日志用 `$uri` 而非含查询串的完整请求，也没有记录 Cookie、Authorization 或响应正文；字段定义见 [Nginx 日志模块](https://nginx.org/en/docs/http/ngx_http_log_module.html)。错误日志仍可能包含请求地址等上下文，分享前检查脱敏，不要为了排查打开会输出敏感请求头的 debug 日志。

静态 `proxy_pass` 的域名会在加载配置时解析；上游地址变化而旧地址失效时，确认 DNS 正常后重新执行 `nginx -t` 并 reload。Nginx 的日志证明服务器访问了哪个上游，插件日志则说明失败位于作品解析还是图片下载；两边按 PID、时间和路径对照即可。

## 8. Cloudflare 橙云部署（推荐）

域名接入 Cloudflare 时，推荐使用 **橙云代理 + WAF IP 白名单 + 源站防火墙**，由边缘限制客户端、源站限制回源连接，不依赖 `X-Forwarded-For` 做业务白名单。

### 开启橙云和 HTTPS 回源

将 `pixiv-proxy.example.com` 的 DNS 记录设为 **已代理／橙云**，移除覆盖该反代路径的 Worker 路由。SSL/TLS 模式使用 **Full (strict)／完全（严格）**，让 Cloudflare 校验源站证书；上文签发的受信任证书可继续使用。参见 [Cloudflare Full (strict) 说明](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/)。

### 在 WAF 限制 AstrBot 出口 IP

在该站点的 WAF 自定义规则中添加以下表达式，动作选择 **Block／阻止**：

```text
(http.host eq "pixiv-proxy.example.com" and not ip.src in {203.0.113.10 2001:db8::10})
```

替换域名和 AstrBot 的实际出口 IP；不使用 IPv6 时移除示例 IPv6。规则覆盖整个反代域名，同时保护作品接口和图片路径。这里的 `ip.src` 由 Cloudflare 判断，无需读取客户端传入的 `X-Forwarded-For`。仅添加一条允许／跳过白名单 IP 的规则不会自动拒绝其他来源，应明确阻止不在白名单中的请求，并检查是否存在提前跳过此规则的配置。做法见 [Cloudflare WAF IP 白名单示例](https://developers.cloudflare.com/waf/custom-rules/use-cases/allow-traffic-from-ips-in-allowlist/)。

### 在源站限制 Cloudflare 回源

在源站安全组和主机防火墙中，仅允许 [Cloudflare 官方 IP 网段](https://www.cloudflare.com/ips/)连接该反代服务的 TCP 443，并拒绝其他公网来源，防止直接访问源站 IP 绕过 WAF。须同时覆盖实际启用的 IPv4／IPv6 入口，并随官方列表更新；不要只放行某次日志中出现的单个节点 IP，也不要保留 AstrBot 公网出口直连 443 的例外。SSH 管理端口保留原有管理规则。配置原则见 [Cloudflare 源站防火墙说明](https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/)。

完成 WAF 和防火墙限制后，移除第 4 节 Nginx `server` 中的以下直连白名单块，其余反代配置保留：

```nginx
    # Allow the actual public egress addresses of the AstrBot host.
    allow 203.0.113.10;
    # allow 2001:db8::10;
    deny all;
```

Nginx 此时看到的是 Cloudflare 节点地址，不能继续按 AstrBot 出口 IP 执行上述 `allow`。也不要将 Cloudflare 网段当成 AstrBot 的业务白名单：**Cloudflare 网段用于源站入口限制，AstrBot 出口 IP 用于 WAF 白名单，两层必须同时生效**。本方案无需新增 `real_ip_header X-Forwarded-For` 或 `set_real_ip_from`；如果已有站点或面板在恢复真实 IP，须检查是否还有基于该地址执行的旧白名单。

按第 6 节分别验证白名单出口可访问、其他出口被 WAF 阻止、源站无法被公网直连，再用于插件。若合法请求返回 Cloudflare 验证页，检查对应安全事件及命中的规则；插件无法完成浏览器挑战。切回灰云时，应先恢复 Nginx 的 AstrBot 出口白名单及对应防火墙限制。

返回 [README](../README.md)。
