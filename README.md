# Pixiv 图片打包下载器

AstrBot 插件，通过 QQ（NapCat / OneBot、`aiocqhttp` 适配器）下载 Pixiv 插画或漫画的全部原图，按作品页数直接发送图片或打包 ZIP，发送到当前群聊或私聊。

## 安装与使用

1. 将插件放入 AstrBot 的 `data/plugins/` 目录，或在 WebUI 通过插件仓库地址安装。
2. 在 WebUI 重载插件，并在插件配置中按需设置 Cookie 和 Worker 代理。
3. 在 QQ 中发送 `/pixiv 作品PID`，例如 `/pixiv 12345678`。示例 PID 仅展示命令格式，不保证作品可访问。命令前缀跟随 AstrBot 的唤醒前缀配置。

仅接收正整数 PID，不支持作品链接、搜索或作者批量下载。支持静态单图、多页插画和漫画，不支持 ugoira 动图。

默认尝试匿名访问。遇到需要登录的作品，可以填写具有查看权限的 Pixiv 账号 Cookie。账号无权查看、作品被删除或触发网站验证时会提示失败。

默认不超过 **3 页**的作品直接按页序发送图片，**4 页及以上**打包发送 ZIP。在 WebUI 中修改“直接发送页数上限”即可调整分界，例如设为 `5` 时，1～5 页直接发送，6 页及以上打包；设为 `0` 则始终打包 ZIP。已有配置在重载插件后也会补入默认值 `3`。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `pixiv_cookie` | 空 | 可选的 Pixiv Cookie 值，不包含 `Cookie:` 前缀，仅用于作品信息请求，启用代理时经 Worker 转发 |
| `image_proxy_base_url` | 空 | 留空时直连；填写后作品信息和原图全部通过同一个 Worker 访问，保留原配置键名 |
| `direct_send_max_pages` | 3 | 作品页数不超过此值时直接发送图片；超过则打包 ZIP，设为 0 始终打包，必须为非负整数 |
| `zip_password` | 空 | 留空不加密；填写后对需要打包的 ZIP 使用 AES-256 加密，不影响按页数直接发图 |
| `request_timeout` | 60 | 单次 HTTP 请求的总超时，单位秒，必须大于 0 |
| `max_download_mb` | 200 | 单个作品原图的累计大小上限，单位 MiB，必须大于 0 |
| `cleanup_interval_hours` | 0 | 定时清理间隔，单位小时，0 关闭；必须为非负整数 |
| `cleanup_retention_hours` | 24 | 定时清理保留的 ZIP 时长，单位小时，0 表示清理全部；必须为非负整数 |

Cookie 可从浏览器开发者工具的 Network 面板中，访问 `www.pixiv.net` 的请求头获取。界面会遮罩 Cookie，但配置文件仍以明文保存；不要分享 Cookie 或包含它的配置文件。Cookie 仅用于作品信息请求：直连时发送给 Pixiv，启用代理时发送给配置的 Worker，再由 Worker 转发给 Pixiv，因此请使用自己部署或可信的 Worker。图片请求不携带 Cookie，插件也不会将 Cookie 写入日志。

### 直连

将 `image_proxy_base_url` 留空。插件下载图片时自动设置：

```http
Referer: https://www.pixiv.net/
```

运行 AstrBot 的网络必须能访问 `www.pixiv.net` 和 `i.pximg.net`。

### Worker 全程代理

将 `image_proxy_base_url` 设为自己的 Worker 地址，例如 `https://images.example.com`。启用后，运行 AstrBot 的 PC 只需能连接这个 Worker 域名，无须直接访问 `www.pixiv.net` 或 `i.pximg.net`。Worker 自身需能访问这两个上游。

作品详情与分页信息分别请求：

```text
https://images.example.com/ajax/illust/123
https://images.example.com/ajax/illust/123/pages
```

Worker 将这些请求转发到 `https://www.pixiv.net` 的相同路径。如果原图是：

```text
https://i.pximg.net/img-original/img/2026/01/01/00/00/00/123_p0.jpg
```

则请求：

```text
https://images.example.com/img-original/img/2026/01/01/00/00/00/123_p0.jpg
```

支持 `https://example.com/pximg` 这样的前缀地址，例如作品详情会请求 `/pximg/ajax/illust/123`。原图查询参数也会保留。服务须直接返回作品 JSON 或图片内容，不能重定向到上游；插件不会跟随重定向，也不会在代理失败后回退直连。配置地址不接受账号密码、查询参数或 URL 片段。

在 Cloudflare Worker 中部署以下转发逻辑，再填入部署地址：

```javascript
export default {
  fetch(request) {
    const url = new URL(request.url);
    // Set to '/pximg' when the configured proxy URL includes that prefix.
    const pathPrefix = '';
    if (pathPrefix) {
      if (!url.pathname.startsWith(`${pathPrefix}/`)) {
        return new Response('Not found', { status: 404 });
      }
      url.pathname = url.pathname.slice(pathPrefix.length);
    }

    const isArtwork = /^\/ajax\/illust\/[0-9]+(?:\/pages)?$/.test(url.pathname);
    url.protocol = 'https:';
    url.hostname = isArtwork ? 'www.pixiv.net' : 'i.pximg.net';
    url.port = '';

    const proxyRequest = new Request(
      url,
      new Request(request, { redirect: 'manual' }),
    );
    proxyRequest.headers.set('Referer', 'https://www.pixiv.net/');
    if (!isArtwork) {
      proxyRequest.headers.delete('Cookie');
    }

    return fetch(proxyRequest);
  },
};
```

绑定在域名根路径时保留 `pathPrefix = ''`；配置地址为 `https://example.com/pximg` 时，将其设为 `'/pximg'`。插件只提供配置入口，不会替你部署 Worker。

**旧版仅转发图片的 Worker／镜像必须更新为上述同时支持作品接口的版本。** 配置项仍使用 `image_proxy_base_url`，无需迁移配置；更新 Worker 后保存配置并重载插件即可。请求头中的 `Referer: https://www.pixiv.net/` 只是头字段，不会让本机连接 Pixiv。

## ZIP 密码（可选）

在 WebUI 的“ZIP 解压密码”中填写密码，保存并重载插件即可启用；留空则关闭。密码保留原样，支持中文，前后空格也是密码的一部分。

密码只用于原本需要打包的作品：例如直接发送上限为 `3` 时，1～3 页仍直接发图，4 页及以上才生成加密 ZIP。若希望所有作品都生成加密 ZIP，可将直接发送页数上限设为 `0`。

加密在发送前完成，本地保留的也是加密 ZIP。插件只提示“加密 ZIP 已发送”，不会将密码发送到聊天、写入日志或放进文件名；请自行告知接收者密码。WebUI 中的密码会遮罩，但仍以明文存储在本机插件配置文件中。

使用支持 AES ZIP 的解压工具打开，例如 7-Zip 或 WinRAR；部分系统自带的解压工具可能不支持。ZIP 中的文件内容加密，文件名仍可查看。安装或升级插件时需安装 `requirements.txt` 中的 `pyzipper` 依赖；在源码部署中可于 AstrBot 项目根目录执行：

```powershell
uv pip install --python ".venv/Scripts/python.exe" -r "data/plugins/astrbot_plugin_pixiv_dowlond/requirements.txt"
```

上述路径适用于 Windows；Linux/macOS 使用 `.venv/bin/python`。

## 文件保存与发送

- 直接发送模式会先下载全部图片，再按页序逐张发送；发送过程中保留临时文件，完成后清理，不生成 ZIP，也不保留本地图片副本。
- 直接发送中某一张失败时停止发送，提示已发送数量；不自动补发或转为 ZIP，避免重复发送。可将页数上限设为 `0` 后重试，使用 ZIP 下载。
- ZIP 保存在 AstrBot 数据目录的 `plugin_data/astrbot_plugin_pixiv_dowlonder/` 下，名称为 `PID_时间_随机后缀.zip`，重复下载不会覆盖旧文件。
- ZIP 内按 `PID_p0.jpg`、`PID_p1.png` 等命名，保留原图内容和格式，不转码。ZIP 只包含该作品的原图。
- 临时图片保存在 AstrBot 临时目录中，每次任务独立，任务完成、失败或正常卸载取消时清理。
- 任意一页下载失败、返回非图片或超过大小上限时终止，不发送图片或生成缺图的 ZIP；已保存的完整 ZIP 默认保留，可使用下述手动或定时清理功能。
- 最多同时处理两个任务，忙碌时提示稍后重试；每个任务按页顺序下载。
- 文件发送失败时会回复保存路径，便于手工取用。上传大小、频率、群文件权限等限制取决于 QQ 和 NapCat，插件的下载上限不保证文件一定能上传。

AstrBot 与 NapCat 需直接运行在同一 PC，使用同一套可读取的本地路径。ZIP 发送使用 AstrBot 的 `File` 消息组件；若后续改为 Docker 或跨机器部署，需要另行配置共享目录或文件访问方案。

## 一键清理与定时清理

AstrBot 管理员发送 `/pixiv_clean`（或 `/pixiv清理`）即可清理全部未使用的插件 ZIP，不受保留时长限制。命令会返回删除数量、释放空间、跳过数量和失败数量；群管理员身份本身不授予此权限，需要配置为 AstrBot 管理员。

在 WebUI 插件配置中设置定时清理，例如：

```json
{
  "cleanup_interval_hours": 6,
  "cleanup_retention_hours": 24
}
```

保存并重载插件后，每 6 小时清理一次超过 24 小时的 ZIP。首次清理在重载后 6 小时执行，不会启动即清理；文件年龄按本地最后修改时间计算。将间隔设为 `0` 关闭定时清理，将保留时长设为 `0` 则每次清理全部未使用的 ZIP。AstrBot 停止时不执行，重启或重载后重新计时，不补跑停机期间的任务。

清理仅删除插件数据目录顶层、符合 `PID_时间_随机后缀.zip` 命名的普通文件，不递归、不跟随符号链接或 Windows 重解析点。正在发送的 ZIP 会跳过，上传失败后保留的 ZIP 在任务结束后也可被清理。其他文件和临时任务目录不受影响。删除的本地 ZIP 不可恢复，QQ 已收到的附件不受影响；定时清理结果记录在插件日志中。

## 开发验证

在 AstrBot 项目根目录执行：

```powershell
uv run python -m pytest "data/plugins/astrbot_plugin_pixiv_dowlond/tests" -q
uv run ruff format --check --no-respect-gitignore --config "exclude=[]" "data/plugins/astrbot_plugin_pixiv_dowlond"
uv run ruff check --no-respect-gitignore --config "exclude=[]" "data/plugins/astrbot_plugin_pixiv_dowlond"
```

测试模拟 Pixiv 和图片服务，不需要真实 Cookie 或外网，覆盖直连、Worker 根路径与路径前缀、Cookie 隔离，以及拒绝重定向和代理失败不回退直连。实际验收还需在 NapCat 中分别验证群聊、私聊，以及直连和已部署 Worker 的完整下载；代理模式可在本机无法连接 Pixiv 的环境中验证。Pixiv AJAX 属于网站接口，可能随网站变动，需要以实际访问结果为准。
