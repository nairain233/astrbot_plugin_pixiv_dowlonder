# Pixiv 图片打包下载器

AstrBot 插件，通过 QQ（NapCat / OneBot、`aiocqhttp` 适配器）下载 Pixiv 插画或漫画的全部原图，按作品页数直接发送图片或打包 ZIP，发送到当前群聊或私聊。

## 安装与使用

1. 将插件放入 AstrBot 的 `data/plugins/` 目录，或在 WebUI 通过插件仓库地址安装。
2. 在 WebUI 重载插件，并在插件配置中按需设置 Cookie 和图片镜像。
3. 在 QQ 中发送 `/pixiv 作品PID`，例如 `/pixiv 12345678`。示例 PID 仅展示命令格式，不保证作品可访问。命令前缀跟随 AstrBot 的唤醒前缀配置。

仅接收正整数 PID，不支持作品链接、搜索或作者批量下载。支持静态单图、多页插画和漫画，不支持 ugoira 动图。

默认尝试匿名访问。遇到需要登录的作品，可以填写具有查看权限的 Pixiv 账号 Cookie。账号无权查看、作品被删除或触发网站验证时会提示失败。

默认不超过 **3 页**的作品直接按页序发送图片，**4 页及以上**打包发送 ZIP。在 WebUI 中修改“直接发送页数上限”即可调整分界，例如设为 `5` 时，1～5 页直接发送，6 页及以上打包；设为 `0` 则始终打包 ZIP。已有配置在重载插件后也会补入默认值 `3`。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `pixiv_cookie` | 空 | 可选的 Pixiv Cookie 值，不包含 `Cookie:` 前缀，仅用于官方作品信息请求 |
| `image_proxy_base_url` | 空 | 留空时直连图片；填写后通过兼容 pximg 路径的 Worker／镜像下载 |
| `direct_send_max_pages` | 3 | 作品页数不超过此值时直接发送图片；超过则打包 ZIP，设为 0 始终打包，必须为非负整数 |
| `request_timeout` | 60 | 单次 HTTP 请求的总超时，单位秒，必须大于 0 |
| `max_download_mb` | 200 | 单个作品原图的累计大小上限，单位 MiB，必须大于 0 |

Cookie 可从浏览器开发者工具的 Network 面板中，访问 `www.pixiv.net` 的请求头获取。界面会遮罩 Cookie，但配置文件仍以明文保存；不要分享 Cookie 或包含它的配置文件。插件不会把 Cookie 发给图片服务器或镜像，也不会将其写入日志。

### 图片直连

将 `image_proxy_base_url` 留空。插件下载图片时自动设置：

```http
Referer: https://www.pixiv.net/
```

运行 AstrBot 的网络必须能访问 `www.pixiv.net` 和 `i.pximg.net`。

### Worker／图片镜像

将 `image_proxy_base_url` 设为自己的服务地址，例如 `https://images.example.com`。如果原图是：

```text
https://i.pximg.net/img-original/img/2026/01/01/00/00/00/123_p0.jpg
```

则请求：

```text
https://images.example.com/img-original/img/2026/01/01/00/00/00/123_p0.jpg
```

支持 `https://example.com/pximg` 这样的前缀地址，原图查询参数也会保留。服务须接受相同路径并直接返回图片。配置地址不接受账号密码、查询参数或 URL 片段。

可在 Cloudflare Worker 中部署以下用户提供的转发逻辑，再填入部署地址：

```javascript
export default {
  fetch(request) {
    const url = new URL(request.url);
    url.hostname = 'i.pximg.net';

    const proxyRequest = new Request(url, request);
    proxyRequest.headers.set('Referer', 'https://www.pixiv.net/');

    return fetch(proxyRequest);
  },
};
```

此示例适用于绑定在域名根路径的 Worker；使用路径前缀部署时，Worker 需先去掉该前缀再转发。插件只提供配置入口，不会替你部署 Worker。

**图片 Worker 不代理作品信息。** 获取作品信息始终访问 Pixiv 官方 AJAX 接口，所以运行 AstrBot 的 PC 仍须能直接访问 `www.pixiv.net`。

## 文件保存与发送

- 直接发送模式会先下载全部图片，再按页序逐张发送；发送过程中保留临时文件，完成后清理，不生成 ZIP，也不保留本地图片副本。
- 直接发送中某一张失败时停止发送，提示已发送数量；不自动补发或转为 ZIP，避免重复发送。可将页数上限设为 `0` 后重试，使用 ZIP 下载。
- ZIP 保存在 AstrBot 数据目录的 `plugin_data/astrbot_plugin_pixiv_dowlonder/` 下，名称为 `PID_时间_随机后缀.zip`，重复下载不会覆盖旧文件。
- ZIP 内按 `PID_p0.jpg`、`PID_p1.png` 等命名，保留原图内容和格式，不转码。ZIP 只包含该作品的原图。
- 临时图片保存在 AstrBot 临时目录中，每次任务独立，任务完成、失败或正常卸载取消时清理。
- 任意一页下载失败、返回非图片或超过大小上限时终止，不发送图片或生成缺图的 ZIP；已保存的完整 ZIP 会一直保留，需要自行按需清理。
- 最多同时处理两个任务，忙碌时提示稍后重试；每个任务按页顺序下载。
- 文件发送失败时会回复保存路径，便于手工取用。上传大小、频率、群文件权限等限制取决于 QQ 和 NapCat，插件的下载上限不保证文件一定能上传。

AstrBot 与 NapCat 需直接运行在同一 PC，使用同一套可读取的本地路径。ZIP 发送使用 AstrBot 的 `File` 消息组件；若后续改为 Docker 或跨机器部署，需要另行配置共享目录或文件访问方案。

## 开发验证

在 AstrBot 项目根目录执行：

```powershell
uv run python -m pytest "data/plugins/astrbot_plugin_pixiv_dowlond/tests" -q
uv run ruff format --check --no-respect-gitignore --config "exclude=[]" "data/plugins/astrbot_plugin_pixiv_dowlond"
uv run ruff check --no-respect-gitignore --config "exclude=[]" "data/plugins/astrbot_plugin_pixiv_dowlond"
```

测试模拟 Pixiv 和图片服务，不需要真实 Cookie 或网络。实际验收还需在 NapCat 中分别验证群聊、私聊，以及直连和已部署 Worker 的原图下载。Pixiv AJAX 属于网站接口，可能随网站变动，需要以实际访问结果为准。
