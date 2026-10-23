import asyncio
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import aiohttp

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path


class PixivDownloadError(Exception):
    """Represent an expected download failure safe to display to the user."""


@register(
    "astrbot_plugin_pixiv_dowlonder",
    "夜雨",
    "按页数直接发送 Pixiv 图片或打包 ZIP",
    "1.1.0",
)
class PixivDownloader(Star):
    """Download a static Pixiv artwork and send images or a ZIP by page count."""

    def __init__(self, context: Context, config: AstrBotConfig):
        """Initialize configuration and track in-flight commands.

        Args:
            context: AstrBot runtime context.
            config: Plugin configuration from the dashboard.
        """
        super().__init__(context)
        self.config = config
        self._tasks: set[asyncio.Task] = set()
        self._stopping = False

    @filter.command("pixiv")
    async def pixiv(self, event: AstrMessageEvent, pid: str = ""):
        """Download all original images for a numeric Pixiv artwork ID.

        Args:
            event: Message event receiving the images or ZIP attachment.
            pid: Positive numeric artwork ID.
        """
        if not re.fullmatch(r"[0-9]{1,20}", pid) or int(pid) == 0:
            await event.send(
                event.plain_result("用法：/pixiv PID，例如 /pixiv 12345678")
            )
            return
        if event.get_platform_name() != "aiocqhttp":
            await event.send(
                event.plain_result("此插件目前仅支持 QQ（NapCat / OneBot）。")
            )
            return
        if self._stopping or len(self._tasks) >= 2:
            await event.send(event.plain_result("下载任务繁忙，请稍后重试。"))
            return

        task = asyncio.current_task()
        self._tasks.add(task)
        pid = str(int(pid))
        archive = None
        try:
            timeout = float(self.config.get("request_timeout", 60))
            limit = float(self.config.get("max_download_mb", 200))
            if not 0 < timeout < float("inf") or not 0 < limit < float("inf"):
                raise PixivDownloadError(
                    "请求超时和下载大小上限必须为正数，请检查插件配置。"
                )
            direct_pages = self.config.get("direct_send_max_pages", 3)
            if (
                not isinstance(direct_pages, int)
                or isinstance(direct_pages, bool)
                or direct_pages < 0
            ):
                raise PixivDownloadError("直接发送页数上限必须是大于等于 0 的整数。")
            proxy = str(self.config.get("image_proxy_base_url", "")).strip().rstrip("/")
            if proxy:
                parsed = urlsplit(proxy)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.query
                    or parsed.fragment
                ):
                    raise PixivDownloadError(
                        "图片代理地址应为 HTTP(S) 地址，可含路径前缀，不能含账号、查询参数或片段。"
                    )
            cookie = str(self.config.get("pixiv_cookie", "")).strip()
            if "\r" in cookie or "\n" in cookie:
                raise PixivDownloadError("Cookie 不能包含换行，请检查插件配置。")

            temp_root = Path(get_astrbot_temp_path())
            temp_root.mkdir(parents=True, exist_ok=True)
            await event.send(
                event.plain_result(f"开始下载 Pixiv 作品 {pid} 的全部原图，请稍候。")
            )
            with tempfile.TemporaryDirectory(prefix="pixiv_", dir=temp_root) as temp:
                work_dir = Path(temp)
                images = work_dir / "images"
                images.mkdir()
                # Explicit request headers and an empty cookie jar isolate login data.
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    cookie_jar=aiohttp.DummyCookieJar(),
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; AstrBotPixiv/1.1)"
                    },
                ) as session:
                    page_count = await self._download(
                        session, pid, images, cookie, proxy, int(limit * 1024 * 1024)
                    )

                if 0 < page_count <= direct_pages:
                    for index, image in enumerate(
                        sorted(
                            images.iterdir(),
                            key=lambda path: int(path.stem.rsplit("_p", 1)[1]),
                        ),
                        start=1,
                    ):
                        try:
                            await event.send(
                                event.chain_result([Image.fromFileSystem(image)])
                            )
                        except Exception as exc:
                            self.logger.warning(
                                "Pixiv image upload failed for PID %s page %s (%s).",
                                pid,
                                index,
                                type(exc).__name__,
                            )
                            await event.send(
                                event.plain_result(
                                    f"作品 {pid} 已下载，但第 {index} 张图片发送失败，已发送 {index - 1}/{page_count} 张。请检查 NapCat 状态，或将直接发送页数上限设为 0 后重试，改用 ZIP。"
                                )
                            )
                            return
                    await event.send(
                        event.plain_result(
                            f"作品 {pid} 下载完成，共 {page_count} 张原图，已直接发送。"
                        )
                    )
                    return

                data_dir = StarTools.get_data_dir("astrbot_plugin_pixiv_dowlonder")
                # Await the worker even on cancellation before removing its input files.
                packing = asyncio.create_task(
                    asyncio.to_thread(
                        shutil.make_archive,
                        str(work_dir / "archive"),
                        "zip",
                        root_dir=images,
                    )
                )
                try:
                    packed = await asyncio.shield(packing)
                except asyncio.CancelledError:
                    await packing
                    raise
                name = f"{pid}_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.zip"
                archive = data_dir / name
                Path(packed).replace(archive)

            try:
                await event.send(
                    event.chain_result([File(name=archive.name, file=str(archive))])
                )
            except Exception as exc:
                self.logger.warning(
                    "Pixiv ZIP upload failed for PID %s (%s).", pid, type(exc).__name__
                )
                await event.send(
                    event.plain_result(
                        f"作品 {pid} 已下载，共 {page_count} 张，但 ZIP 发送失败。请检查 NapCat 的文件权限和本地路径访问。文件保留在：{archive}"
                    )
                )
                return
            await event.send(
                event.plain_result(
                    f"作品 {pid} 下载完成，共 {page_count} 张原图。ZIP 已发送并保留在本地。"
                )
            )
        except PixivDownloadError as exc:
            await event.send(event.plain_result(f"下载失败：{exc}"))
        except asyncio.TimeoutError:
            await event.send(
                event.plain_result(
                    "下载失败：请求超时，请检查网络或图片代理，或调大请求超时。"
                )
            )
        except aiohttp.ClientError as exc:
            self.logger.warning(
                "Pixiv network failure for PID %s (%s).", pid, type(exc).__name__
            )
            await event.send(
                event.plain_result(
                    "下载失败：网络请求异常，请检查 Pixiv 连通性及图片代理配置。"
                )
            )
        except (TypeError, ValueError):
            await event.send(
                event.plain_result(
                    "下载失败：插件配置或 Pixiv 响应格式无效，请检查配置后重试。"
                )
            )
        except OSError as exc:
            self.logger.warning(
                "Pixiv storage failure for PID %s (%s).", pid, type(exc).__name__
            )
            await event.send(
                event.plain_result(
                    "下载失败：无法写入或打包文件，请检查磁盘空间及目录权限。"
                )
            )
        except Exception as exc:
            self.logger.error(
                "Unexpected Pixiv failure for PID %s (%s).", pid, type(exc).__name__
            )
            message = "处理失败，请查看插件日志。"
            if archive is not None and archive.is_file():
                message += f" ZIP 已保留在：{archive}"
            await event.send(event.plain_result(message))
        finally:
            self._tasks.discard(task)

    async def _download(
        self,
        session: aiohttp.ClientSession,
        pid: str,
        images: Path,
        cookie: str,
        proxy: str,
        limit: int,
    ) -> int:
        """Resolve artwork pages and stream every original into a task directory.

        Args:
            session: HTTP session without shared authentication headers.
            pid: Validated numeric artwork ID.
            images: Empty directory belonging to this request.
            cookie: Optional login cookie sent only to the official AJAX API.
            proxy: Optional image proxy base URL, including any path prefix.
            limit: Maximum cumulative number of original-image bytes.

        Returns:
            Number of successfully downloaded pages.

        Raises:
            PixivDownloadError: The work is inaccessible or an image is invalid.
            aiohttp.ClientError: An HTTP transport error occurs.
        """
        headers = {"Referer": "https://www.pixiv.net/"}
        if cookie:
            headers["Cookie"] = cookie
        pages = []
        for suffix in ("", "/pages"):
            async with session.get(
                f"https://www.pixiv.net/ajax/illust/{pid}{suffix}",
                headers=headers,
                allow_redirects=False,
            ) as response:
                if response.status == 429:
                    raise PixivDownloadError("Pixiv 请求过于频繁，请稍后重试。")
                if response.status in {401, 403}:
                    raise PixivDownloadError(
                        "Pixiv 拒绝访问，请检查登录 Cookie 是否有效及账号是否有权查看作品。"
                    )
                if response.status == 404:
                    raise PixivDownloadError("作品不存在、已删除或不可访问。")
                if 300 <= response.status < 400:
                    raise PixivDownloadError(
                        "Pixiv 接口发生跳转，可能需要登录；请检查 Cookie。"
                    )
                if response.status != 200:
                    raise PixivDownloadError(
                        f"Pixiv 作品接口返回 HTTP {response.status}，请稍后重试。"
                    )
                if response.content_type != "application/json":
                    raise PixivDownloadError(
                        "Pixiv 未返回作品数据，可能需要登录或遇到访问验证。"
                    )
                payload = await response.json()
            if not isinstance(payload, dict) or payload.get("error"):
                raise PixivDownloadError(
                    "作品不可访问，请检查 PID、登录 Cookie 及账号查看权限。"
                )
            body = payload.get("body")
            if not suffix:
                if not isinstance(body, dict) or body.get("illustType") not in {
                    0,
                    1,
                    2,
                }:
                    raise PixivDownloadError("Pixiv 作品详情格式异常，请稍后重试。")
                if body["illustType"] == 2:
                    raise PixivDownloadError(
                        "暂不支持 Pixiv 动图（ugoira），请下载静态插画或漫画。"
                    )
            else:
                if not isinstance(body, list) or not body:
                    raise PixivDownloadError("该作品没有可下载的原图。")
                pages = body

        total = 0
        for index, page in enumerate(pages):
            urls = page.get("urls") if isinstance(page, dict) else None
            original = urls.get("original") if isinstance(urls, dict) else None
            if not isinstance(original, str):
                raise PixivDownloadError("Pixiv 原图地址缺失。")
            parsed = urlsplit(original)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "i.pximg.net"
                or Path(parsed.path).suffix.lower()
                not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
            ):
                raise PixivDownloadError("Pixiv 返回了不支持的原图地址或格式。")
            image_url = original
            if proxy:
                image_url = proxy + urlunsplit(("", "", parsed.path, parsed.query, ""))
            path = images / f"{pid}_p{index}{Path(parsed.path).suffix.lower()}"
            async with session.get(
                image_url, headers={"Referer": "https://www.pixiv.net/"}
            ) as response:
                if response.status == 429:
                    raise PixivDownloadError("图片服务器请求过于频繁，请稍后重试。")
                if response.status == 403:
                    raise PixivDownloadError(
                        "图片服务器拒绝访问，请检查 Referer 或图片代理配置。"
                    )
                if response.status != 200:
                    raise PixivDownloadError(
                        f"第 {index + 1} 张图片下载失败（HTTP {response.status}），未生成 ZIP。"
                    )
                if not response.content_type.startswith("image/"):
                    raise PixivDownloadError(
                        "图片服务器返回的不是图片，请检查 Worker／镜像配置。"
                    )
                if (
                    response.content_length is not None
                    and total + response.content_length > limit
                ):
                    raise PixivDownloadError("作品原图总大小超过配置上限，未生成 ZIP。")
                size = 0
                with path.open("wb") as output:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        size += len(chunk)
                        if total > limit:
                            raise PixivDownloadError(
                                "作品原图总大小超过配置上限，未生成 ZIP。"
                            )
                        output.write(chunk)
                if not size:
                    raise PixivDownloadError(f"第 {index + 1} 张图片为空，未生成 ZIP。")
        return len(pages)

    async def terminate(self):
        """Cancel active downloads and wait for temporary-file cleanup."""
        self._stopping = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
