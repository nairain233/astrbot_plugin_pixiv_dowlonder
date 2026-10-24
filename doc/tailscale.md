# Tailscale 内网互联与 FastAPI 反代

让运行 AstrBot 的机器和境外 VPS 加入同一 tailnet，通过 Tailscale 地址直接访问 **FastAPI + Uvicorn 的 HTTP 服务**。AstrBot 机器不需要直接访问 Pixiv，VPS 上的反代端口也不需要向公网开放。本文适用于 Windows 运行 AstrBot、Debian／Ubuntu 运行反代的场景，不需要安装 Nginx。

如果选择公网域名和 HTTPS，使用 [Nginx 公网部署教程](nginx-proxy.md)，由 Nginx 直接转发 Pixiv；该方案保留证书、IP 白名单和 Cloudflare 橙云说明。两套方案使用相同的插件字段与路径，按实际部署选择，不根据协议自动切换服务。

## 1. 网络结构和地址约定

```text
AstrBot 机器 100.101.102.10
    │ Tailscale 加密隧道，连接 TCP 8080
    ▼
境外 VPS 100.101.102.103:8080（FastAPI + Uvicorn）
    ├─ /pixiv/ajax/illust/... → HTTPS www.pixiv.net
    └─ /pximg/图片路径       → HTTPS i.pximg.net
```

本文的两个 `100.x` 地址只是示例，须换成 `tailscale ip -4` 显示的实际地址。两端都是 tailnet 节点时，不需要子网路由、不需要出口节点，不要为了这个反代给 AstrBot 开启全局出口代理。Tailscale 只负责两台机器之间的连接，VPS 自己仍须能够访问 Pixiv 的作品接口和图片上游。

Tailscale 使用加密连接，无法直连时可能经中继转发；HTTP 应用流量在节点之间的隧道里传输，不要求为裸 IP 签发 HTTPS 证书。不过 FastAPI 所在机器仍能看到解密后的请求与 Cookie，应使用自己管理或可信任的服务器。Tailscale 的基础安装流程见[官方快速开始](https://tailscale.com/docs/how-to/quickstart)，加密原理见[官方安全说明](https://tailscale.com/security)。

## 2. 在两台机器上加入同一 tailnet

### 境外 Debian／Ubuntu 服务器

按官方安装方式执行：

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo systemctl enable --now tailscaled
sudo tailscale up
tailscale ip -4
tailscale status
```

打开 `tailscale up` 给出的登录链接，在浏览器中登录自己的 Tailscale 账号，完成这台机器的授权。记录 VPS 的 Tailscale IPv4，下文用 `100.101.102.103` 代替。安装来源见 [Linux 下载与安装](https://tailscale.com/download/linux)。

### 运行 AstrBot 的 Windows 机器

从 [Tailscale Windows 安装说明](https://tailscale.com/docs/install/windows)下载客户端，安装后在托盘菜单登录**同一个 tailnet**。如果账号属于多个组织，确认选中了与 VPS 相同的网络。管理后台启用了设备审批时，还须批准两台设备。

安装后重新打开 PowerShell，执行：

```powershell
tailscale ip -4
tailscale status
tailscale ping 100.101.102.103
```

若提示找不到 `tailscale`，可使用完整路径，例如 `& "$env:ProgramFiles/Tailscale/tailscale.exe" ip -4`。记录 AstrBot 机器的 Tailscale IP，下文用 `100.101.102.10` 代替。希望 Windows 注销用户后仍保持连接，可在客户端启用无人值守运行，具体参见 [Tailscale CLI 的 `--unattended` 说明](https://tailscale.com/docs/reference/tailscale-cli#up)。

如果 AstrBot 也运行在 Linux，按 VPS 的安装步骤加入同一 tailnet即可。`tailscale ping` 成功仅说明节点连接可用，还需要实际测试 TCP 8080 和 HTTP 服务。

## 3. 只允许 AstrBot 访问反代端口

这里使用两道限制：Tailscale 网络策略允许 AstrBot 连接 VPS 的 TCP 8080；FastAPI 再按真实连接来源匹配 AstrBot 的 Tailscale IP 白名单。

在 Tailscale 管理后台的访问控制策略中，下面是一条只允许该连接的 `grants` 示例：

```json
{
  "grants": [
    {
      "src": ["100.101.102.10"],
      "dst": ["100.101.102.103"],
      "ip": ["tcp:8080"]
    }
  ]
}
```

该示例表示 AstrBot 节点可发起连接，响应流量随已建立的连接返回。语法见 [Tailscale grants 说明](https://tailscale.com/docs/reference/syntax/grants)。修改现有网络时应合并所需规则，保留 SSH 等管理连接的必要权限，并在后台验证策略后保存，不要直接覆盖整个组织的策略。

**授权规则是累加放行的**：如果仍保留允许所有设备互访的旧 ACL 或 grant，新增这条窄规则并不会撤销旧授权。要达到“只有 AstrBot 可访问 8080”，还须调整覆盖该端口的宽泛授权；FastAPI 的 IP 白名单也应保留。IP 规则仅针对示例 IPv4，换用 Tailscale IPv6 时须同步修改策略、监听和白名单。

## 4. 在 VPS 部署 FastAPI

先确认 `tailscale ip -4` 能看到 VPS 地址，并按 [Nginx 教程的上游验证步骤](nginx-proxy.md#3-先验证境外服务器能获取作品信息)确认 VPS 能取得公开作品 JSON。再安装工具：

```bash
sudo apt update
sudo apt install python3 python3-venv ca-certificates curl
mkdir -p "$HOME/pixiv-tailnet"
cd "$HOME/pixiv-tailnet"
python3 -m venv ".venv"
".venv/bin/python" -m pip install "fastapi>=0.118,<1" "uvicorn>=0.30,<1" "aiohttp>=3.11.18,<4"
```

下面是完整服务代码，保存到当前目录的 `app.py`。依赖安装在 VPS 的独立虚拟环境，**不需要修改 AstrBot 插件的 requirements.txt，也不需要在 AstrBot 中导入此服务**。代码只接受 GET／HEAD，并固定两个上游，不能用来代理任意网址。

```python
import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from ipaddress import ip_address

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from yarl import URL

logger = logging.getLogger("uvicorn.error")
RESPONSE_HEADERS = {
    "content-type",
    "content-length",
    "content-encoding",
    "content-range",
    "accept-ranges",
    "cache-control",
    "etag",
    "last-modified",
    "retry-after",
    "location",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create the shared pool and validate the required client allowlist.

    Args:
        app: FastAPI application owning the connection pool.

    Yields:
        Control while the application is running.

    Raises:
        RuntimeError: The client allowlist is missing or invalid.
    """
    try:
        app.state.allowed = {
            ip_address(value.strip())
            for value in os.environ.get("PIXIV_ALLOWED_IPS", "").split(",")
        }
    except ValueError:
        raise RuntimeError(
            "Set PIXIV_ALLOWED_IPS to comma-separated client IPs."
        ) from None
    app.state.slots = asyncio.BoundedSemaphore(2)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60, connect=10),
        connector=aiohttp.TCPConnector(limit=2),
        cookie_jar=aiohttp.DummyCookieJar(),
        auto_decompress=False,
        trust_env=False,
    ) as session:
        app.state.session = session
        yield


class ProxyStream(StreamingResponse):
    """Own the upstream response and concurrency slot through the entire send."""

    def __init__(self, upstream, slots, path, started):
        """Prepare a streaming response without buffering the upstream body.

        Args:
            upstream: Open aiohttp response whose bytes must remain unmodified.
            slots: Semaphore already acquired by the request handler.
            path: Local request path without its query string.
            started: Monotonic request start time for logging.
        """
        self.upstream = upstream
        self.slots = slots
        self.path = path
        self.started = started
        super().__init__(
            upstream.content.iter_chunked(64 * 1024),
            status_code=upstream.status,
            headers={
                key: value
                for key, value in upstream.headers.items()
                if key.lower() in RESPONSE_HEADERS
            },
        )

    async def __call__(self, scope, receive, send):
        """Close upstream sockets and release capacity on every termination path.

        Args:
            scope: ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.

        Raises:
            RuntimeError: An upstream stream fails after response headers are sent.
            asyncio.CancelledError: The response task is cancelled.
        """
        stage = "complete"
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            stage = "cancelled"
            raise
        except Exception as exc:
            stage = "stream_error"
            logger.warning(
                "path=%r stage=stream error=%s", self.path, type(exc).__name__
            )
            raise RuntimeError("Upstream stream interrupted.") from None
        finally:
            self.upstream.close()
            self.slots.release()
            logger.info(
                "path=%r upstream_status=%s stage=%s elapsed=%.3f",
                self.path,
                self.upstream.status,
                stage,
                time.monotonic() - self.started,
            )


app = FastAPI(
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    redirect_slashes=False,
)


@app.api_route("/{path:path}", methods=["GET", "HEAD"])
async def proxy(request: Request, path: str):
    """Forward the two supported routes using only explicitly permitted headers.

    Args:
        request: Request with the real peer address supplied by Uvicorn.
        path: Local URL path without its leading slash.

    Returns:
        An upstream stream or a local JSON error response.
    """
    started = time.monotonic()
    try:
        permitted = request.client is not None and (
            ip_address(request.client.host) in request.app.state.allowed
        )
    except ValueError:
        permitted = False
    if not permitted:
        logger.warning(
            "path=%r upstream_status=- stage=access status=403", request.url.path
        )
        return JSONResponse({"detail": "Client IP is not allowed."}, status_code=403)

    match = re.fullmatch(r"pixiv/ajax/illust/([0-9]{1,20})(/pages)?", path)
    if match and int(match[1]) > 0:
        host, prefix = "www.pixiv.net", "/pixiv"
        referer = f"https://www.pixiv.net/artworks/{match[1]}"
    elif path.startswith("pximg/") and len(path) > len("pximg/"):
        host, prefix = "i.pximg.net", "/pximg"
        referer = "https://www.pixiv.net/"
    else:
        return JSONResponse({"detail": "Unknown proxy path."}, status_code=404)

    # Use raw, already-encoded bytes to preserve query escapes and repeated keys.
    raw_path = request.scope["raw_path"].decode("ascii")
    query = request.scope["query_string"].decode("ascii")
    if not raw_path.startswith(prefix + "/"):
        return JSONResponse({"detail": "Invalid path prefix."}, status_code=400)
    target = URL(
        f"https://{host}{raw_path[len(prefix) :]}" + (f"?{query}" if query else ""),
        encoded=True,
    )
    headers = {
        name: request.headers[name]
        for name in (
            "User-Agent",
            "Accept",
            "Accept-Language",
            "Accept-Encoding",
            "Range",
            "If-Range",
            "If-None-Match",
            "If-Modified-Since",
        )
        if name in request.headers
    }
    headers["Referer"] = referer
    if host == "www.pixiv.net" and "cookie" in request.headers:
        headers["Cookie"] = request.headers["cookie"]

    slots = request.app.state.slots
    if slots.locked():
        logger.warning(
            "path=%r upstream_status=- stage=capacity status=503", request.url.path
        )
        return JSONResponse(
            {"detail": "Proxy is busy; try again later."}, status_code=503
        )
    await slots.acquire()
    upstream = None
    try:
        upstream = await request.app.state.session.request(
            request.method,
            target,
            headers=headers,
            allow_redirects=False,
        )
        return ProxyStream(upstream, slots, request.url.path, started)
    except asyncio.TimeoutError:
        status, detail, stage = 504, "Upstream request timed out.", "timeout"
    except aiohttp.ClientError:
        status, detail, stage = 502, "Upstream connection failed.", "connect"
    except BaseException:
        if upstream is not None:
            upstream.close()
        slots.release()
        raise
    if upstream is not None:
        upstream.close()
    slots.release()
    logger.warning(
        "path=%r upstream_status=- stage=%s status=%s elapsed=%.3f",
        request.url.path,
        stage,
        status,
        time.monotonic() - started,
    )
    return JSONResponse({"detail": detail}, status_code=status)
```

示例使用 [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/) 管理共享连接池，用 [StreamingResponse](https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse) 逐块转发，并在 ASGI 响应生命周期结束时关闭上游、释放名额。[aiohttp ClientSession](https://docs.aiohttp.org/en/stable/client_reference.html#client-session) 设置空 CookieJar 和关闭自动解压，避免跨请求保存登录信息，以及压缩响应头与正文不匹配。响应头只保留选定的内容、缓存及重定向元数据，不转发 Set-Cookie 或逐跳头。

### 启动和后台运行

先在当前终端启动，白名单填写 AstrBot 的 Tailscale IP，可用逗号分隔多个 IP；缺失、空项、非法 IP 或 CIDR 都会拒绝启动：

```bash
export PIXIV_ALLOWED_IPS="100.101.102.10"
".venv/bin/python" -m uvicorn app:app --host 100.101.102.103 --port 8080 --workers 1 --no-proxy-headers --no-access-log
```

将 `--host` 替换为 VPS 实际 Tailscale IP，白名单中填客户端 IP；不要混淆两者。使用 **1 个 worker**，关闭 `--reload`，才能让示例的上限在整个服务中保持 2 个请求。`--no-proxy-headers` 确保白名单读取真实连接来源，客户端伪造 `X-Forwarded-For` 不会改变判断；`--no-access-log` 避免默认访问日志写入完整查询串。

验证可用后按 Ctrl+C 停止前台服务。需要退出 SSH 后继续运行，可仍在当前目录执行：

```bash
nohup ".venv/bin/python" -m uvicorn app:app --host 100.101.102.103 --port 8080 --workers 1 --no-proxy-headers --no-access-log > "proxy.log" 2>&1 &
printf '%s\n' "$!" > "proxy.pid"
ss -lntp 'sport = :8080'
```

`nohup` 继承上面导出的白名单；新开终端启动时先重新设置环境变量。此方式不负责开机自启。停止时先确认 `proxy.pid` 对应的是本服务进程，再发送 SIGTERM：

```bash
ps -p "$(cat "proxy.pid")" -o pid,args
kill -TERM "$(cat "proxy.pid")"
```

监听结果应是实际 Tailscale IP 的 `:8080`，而非 `0.0.0.0:8080` 或 `[::]:8080`。本例不需要在公网安全组开放 8080，也不需要入站证书；系统防火墙如有额外限制，应允许来自 tailnet 的所需连接。Tailscale 自身仍需要正常联网。

若报 `Cannot assign requested address`，检查 VPS 的 Tailscale IP 和连接状态；重启后应等 `tailscale ip -4` 确认地址已就绪再启动服务，不必改为公网通配监听。若此前部署过旧的 Tailscale Nginx 示例，应先停用其相同 IP:8080 的监听，避免端口冲突；已有其他 Nginx 站点无需改动。

### 缓存、并发和超时

服务最多同时处理 2 个上游请求，**图片流发送期间也占用名额**，超额立即返回 503，不建立无限等待队列。连接超时 10 秒、每次上游请求总超时 60 秒；连接失败返回 502，收到上游响应头前超时返回 504。响应头已经发送后若读取失败，终止该响应并记录 `stage=stream`／`stream_error`，不会在图片中追加错误 JSON，也不能再修改已经发送的 HTTP 状态码。

Pixiv 的 401／403／429、非 JSON 内容和 3xx 都原样返回，由插件给出对应阶段提示；服务不跟随跳转、不无限重试。作品详情和分页 JSON 原样转发，图片链接仍由插件的下载反代配置替换。10 分钟元数据缓存、健康模式及下载任务上限继续由插件负责，此服务不重复缓存 Cookie 对应的作品信息。

## 5. 在插件中填写 IP 和端口

```json
{
  "artwork_proxy_base_url": "100.101.102.103:8080/pixiv",
  "image_proxy_base_url": "100.101.102.103:8080/pximg"
}
```

插件会分别请求 `http://100.101.102.103:8080/pixiv/ajax/illust/PID` 和 `http://100.101.102.103:8080/pximg/图片路径`。也可显式填写 `http://`，保存并重载插件。只反代信息时可将图片配置留空或继续使用自己的图片 Worker。

两项地址都遵循同一规则：

| 输入 | 使用的地址 |
| --- | --- |
| `100.101.102.103:8080/pixiv` | `http://100.101.102.103:8080/pixiv` |
| `[fd7a:115c:a1e0::1234]:8080/pximg` | `http://[fd7a:115c:a1e0::1234]:8080/pximg` |
| `proxy.example.com:8443/pixiv` | `https://proxy.example.com:8443/pixiv` |
| `https://100.101.102.103:8443/pixiv` | 保持显式 HTTPS，服务端须提供可验证且匹配该 IP 的证书 |

所有 IP 字面量默认 HTTP，域名默认 HTTPS；这不代表插件会识别是否已连接 Tailscale。如果使用 MagicDNS 名称访问本例 HTTP 服务，应显式填 `http://机器名称:8080/pixiv`。使用半角冒号，IPv6 要加方括号。原先裸 IP 配置如果确实使用 HTTPS，需要补上 `https://` 保持原协议；公网传输 Cookie 应继续使用可信 HTTPS。

## 6. 验证连接和排查问题

在运行 AstrBot 的 Windows 机器上执行，替换 IP 和 PID：

```powershell
tailscale ping 100.101.102.103
Test-NetConnection 100.101.102.103 -Port 8080
curl.exe -sS -D - -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0" -H "Referer: https://www.pixiv.net/artworks/12345678" -H "Accept: application/json" -H "Accept-Language: zh-CN,zh;q=0.8,zh-TW;q=0.2" "http://100.101.102.103:8080/pixiv/ajax/illust/12345678"
```

公开静态作品应返回 HTTP 200、JSON 且 `error=false`。然后在相同终端测试分页：

```powershell
curl.exe -sS -D - -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0" -H "Accept: application/json" "http://100.101.102.103:8080/pixiv/ajax/illust/12345678/pages?lang=zh"
```

从返回的 `urls.original` 复制真实图片链接，把 `https://i.pximg.net/` 替换为 `http://100.101.102.103:8080/pximg/`，保留实际路径和查询参数，再进行 GET 检查响应头：

```powershell
curl.exe -sS -D - -o NUL "http://100.101.102.103:8080/pximg/实际图片路径"
```

预期 HTTP 200、`Content-Type: image/...`，不要直接使用占位路径。从未加入白名单的另一台 tailnet 设备访问有效作品接口，应被网络策略阻止或由服务返回 403。最后在 QQ 执行 `/pixiv 实际PID`，选公开的全年龄静态作品；默认健康模式拦截成人作品时不会继续请求分页和图片。

在 VPS 看上游日志：

```bash
tail -f "proxy.log"
```

| 现象 | 排查方向 |
| --- | --- |
| `tailscale ping` 失败 | 两端是否在同一 tailnet、在线且已审批；查看 `tailscale status` 和 `tailscale netcheck` |
| ping 成功但 TCP 8080 不通 | Uvicorn 是否运行并监听正确地址、grants／ACL 是否放行端口、VPS 是否阻止传入连接、系统防火墙是否另有限制 |
| 403 且日志 `stage=access`、`upstream_status=-` | FastAPI 白名单不符；应填写 AstrBot 的 Tailscale IP，而非公网 IP，修改后重启服务 |
| `upstream_status=403` 或 429 | 请求已到 Pixiv，排查 VPS 出口、作品权限及上游限流；Tailscale 不改变 VPS 到 Pixiv 的访问结果 |
| 503 且 `stage=capacity` | 两个名额正在使用；等待请求结束，确认没有未结束的图片传输 |
| 502／`stage=connect` | 检查 VPS 的 DNS、连接和 HTTPS 证书校验；不会输出含凭据的原始异常消息 |
| 504／`stage=timeout` | 在收到上游响应头前达到请求超时；插件自身也有单请求超时，先到达的一方可能先结束连接 |
| `stage=stream`／`stream_error` 或不完整图片 | 上游中途断流、读取超时或客户端发送失败；服务会释放连接和名额，由插件终止该任务 |
| SSL 握手错误 | 本例入站只提供 HTTP，确认插件填的是 IP 或显式 `http://`；MagicDNS 裸名称会默认 HTTPS |
| 重载后第一次成功，后续看不到作品请求 | 可能命中插件的 10 分钟元数据缓存；图片仍重新下载。需要立刻重测作品接口可直接用 curl 或重载插件 |
| 只有 Tailscale 中继连接，下载较慢 | 中继也可正常工作，但性能可能不同；使用 `tailscale netcheck` 排查网络，勿将应用端口开放公网来代替诊断 |

应用日志仅记录路径、上游状态码、失败阶段及耗时，不记录 Cookie、查询串或响应正文。不要分享 Cookie、认证链接、Auth Key 或包含凭据的诊断输出。管理后台有节点密钥到期策略时应关注设备在线状态；删除并重新注册节点后，也要重新核对 IP、白名单和监听地址。

如果 AstrBot 放在 Docker／虚拟机中，宿主机已连接 Tailscale 不代表容器或来宾系统自动可达：需要从 AstrBot 实际运行环境测试该地址，并按实际连接来源设置白名单。本文不使用 Tailscale Funnel，也不会把反代服务发布到公网。

返回 [README](../README.md) · [公网 Nginx 部署教程](nginx-proxy.md)。
