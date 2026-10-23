# Pixiv 图片打包下载器

AstrBot 插件，通过 QQ（NapCat / OneBot、`aiocqhttp` 适配器）下载 Pixiv 插画或漫画的全部页面，可选原图或较小尺寸图片，按作品页数直接发送图片或打包 ZIP，发送到当前群聊或私聊。

## 安装与使用

1. 将插件放入 AstrBot 的 `data/plugins/` 目录，或在 WebUI 通过插件仓库地址安装。
2. 在 WebUI 重载插件，并在插件配置中按需设置 Cookie 和图片 Worker 代理。
3. 在 QQ 中发送 `/pixiv 作品PID`，例如 `/pixiv 12345678`。示例 PID 仅展示命令格式，不保证作品可访问。命令前缀跟随 AstrBot 的唤醒前缀配置。

仅接收正整数 PID，不支持作品链接、搜索或作者批量下载。支持静态单图、多页插画和漫画，不支持 ugoira 动图。

默认尝试匿名访问。遇到需要登录的作品，可以填写具有查看权限的 Pixiv 账号 Cookie。账号无权查看、作品被删除或触发网站验证时会提示失败。

默认不超过 **3 页**的作品直接按页序发送图片，**4 页及以上**打包发送 ZIP。在 WebUI 中修改“直接发送页数上限”即可调整分界，例如设为 `5` 时，1～5 页直接发送，6 页及以上打包；设为 `0` 则始终打包 ZIP。已有配置在重载插件后也会补入默认值 `3`。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `pixiv_cookie` | 空 | 可选的 Pixiv Cookie 值，不包含 `Cookie:` 前缀，仅发送给 Pixiv 官方作品信息接口 |
| `image_proxy_base_url` | 空 | 留空时直连图片；填写后仅通过 Worker 下载图片，作品信息始终直连 Pixiv |
| `direct_send_max_pages` | 3 | 作品页数不超过此值时直接发送图片；超过则打包 ZIP，设为 0 始终打包，必须为非负整数 |
| `image_size` | `original` | 图片尺寸：`original` 原图、`1200` 约 1200 像素、`540` 约 540 像素，直接请求所选版本 |
| `zip_password` | 空 | 留空不加密；填写后对需要打包的 ZIP 使用 AES-256 加密，不影响按页数直接发图 |
| `request_timeout` | 60 | 单次 HTTP 请求的总超时，单位秒，必须大于 0 |
| `max_download_mb` | 200 | 单个作品所选尺寸图片的累计下载大小上限，单位 MiB，必须大于 0 |
| `cleanup_interval_hours` | 0 | 定时清理间隔，单位小时，0 关闭；必须为非负整数 |
| `cleanup_retention_hours` | 24 | 定时清理保留的 ZIP 时长，单位小时，0 表示清理全部；必须为非负整数 |

Cookie 可从浏览器开发者工具的 Network 面板中，访问 `www.pixiv.net` 的请求头获取。界面会遮罩 Cookie，但配置文件仍以明文保存；不要分享 Cookie 或包含它的配置文件。Cookie 仅直连发送给 Pixiv 官方作品信息接口，不发送给图片 Worker、镜像或图片服务器，插件也不会将 Cookie 写入日志。

### 直连

将 `image_proxy_base_url` 留空。插件下载图片时自动设置：

```http
Referer: https://www.pixiv.net/
```

运行 AstrBot 的网络必须能访问 `www.pixiv.net` 和 `i.pximg.net`。

### Worker 仅代理图片

将 `image_proxy_base_url` 设为自己的 Worker 地址，例如 `https://images.example.com`。启用后，运行 AstrBot 的 PC 仍须能访问 `www.pixiv.net` 和 Worker 域名，无须直接访问图片服务器 `i.pximg.net`。Worker 只向 `i.pximg.net` 转发图片请求，不请求 Pixiv 作品信息。

作品详情与分页信息始终直连：

```text
https://www.pixiv.net/ajax/illust/123
https://www.pixiv.net/ajax/illust/123/pages?lang=zh
```

例如，原图地址为：

```text
https://i.pximg.net/img-original/img/2026/01/01/00/00/00/123_p0.jpg
```

则请求：

```text
https://images.example.com/img-original/img/2026/01/01/00/00/00/123_p0.jpg
```

支持 `https://example.com/pximg` 这样的前缀地址，例如图片会请求 `/pximg/img-original/...`。图片查询参数也会保留。服务须直接返回图片内容，不能重定向到上游；插件不会跟随重定向，也不会在图片代理失败后回退直连图片服务器。配置地址不接受账号密码、查询参数或 URL 片段。

在 Cloudflare Worker 中部署以下精简转发代码，再填入部署地址：

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

此示例适用于绑定在域名根路径的 Worker；使用 `/pximg` 等路径前缀时，Worker 需先去掉前缀再转发。插件只提供配置入口，不会替你部署 Worker。

配置项仍使用 `image_proxy_base_url`，原图片 Worker／镜像地址可继续使用，无需迁移配置。若此前部署的是全程代理 Worker，可替换为上述精简代码。保存配置并重载插件后，链路为：

```text
作品信息：AstrBot → www.pixiv.net
图片下载：AstrBot → 图片 Worker → i.pximg.net
```

### 作品接口返回 403

图片服务 `i.pximg.net` 与作品接口 `www.pixiv.net` 的访问限制不同，图片可下载不代表详情和分页接口可访问。作品接口返回 403 时，请检查运行 AstrBot 的电脑能否直接访问 Pixiv，以及作品权限和登录状态；更换图片 Worker 不会改变作品信息请求的出口。

作品请求失败时，插件会记录 PID、失败阶段（`details`／`pages`）、访问方式（固定为 `direct`）、HTTP 状态码和响应类型，不记录 Cookie 或响应正文。401 提示检查登录状态；403 会区分 JSON 响应和 HTML 等非 JSON 响应，避免将上游拦截一律解释为 Cookie 无效。

## 图片尺寸与省流量下载

在 WebUI 的“下载图片尺寸”中选择：

| 档位 | Pixiv 返回的图片版本 | 用途 |
| --- | --- | --- |
| 原图（默认） | `original` | 下载原始文件，不压缩 |
| 1200 像素 | `regular` | 最长边约 1200 像素的预览图 |
| 540 像素 | `small` | 最长边约 540 像素的预览图，适合节省流量 |

保存并重载插件后生效。插件直接请求 Pixiv 分页接口给出的所选版本地址，不先下载原图，也不进行本地缩放或转码。档位不是精确的输出尺寸，实际尺寸和格式由 Pixiv 提供，保留完整画面；预览图的格式可能与原图不同，例如原图为 PNG，预览图为 JPEG。文件后缀跟随实际请求的图片地址。

此设置同时影响直接发送的图片、普通 ZIP 和加密 ZIP 的内容；Worker 代理也会转发对应的预览图路径。大小限制按实际下载字节计算。所选版本缺失或下载失败时会提示错误，不会自动改下原图，避免额外流量；可手动切换其他档位重试。

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
- ZIP 内按 `PID_p0.jpg`、`PID_p1.png` 等命名，保留所选版本的内容和格式，不转码。ZIP 只包含该作品所选尺寸的图片。
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

测试模拟 Pixiv 和图片服务，不需要真实 Cookie 或外网，覆盖作品接口始终直连、图片直连和 Worker 根路径与路径前缀、Cookie 隔离，以及拒绝重定向和图片代理失败不回退直连。实际验收还需在 NapCat 中分别验证群聊、私聊，以及图片直连和已部署 Worker 的完整下载；两种模式都需要本机能访问 Pixiv 作品接口。Pixiv AJAX 属于网站接口，可能随网站变动，需要以实际访问结果为准。
