import asyncio
import re
import shutil
import stat
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import aiohttp
import pyzipper

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

IMAGE_SIZES = {
    "original": ("original", "原图"),
    "1200": ("regular", "1200 像素预览图"),
    "540": ("small", "540 像素预览图"),
}


class PixivDownloadError(Exception):
    """Represent an expected download failure safe to display to the user."""


@register(
    "astrbot_plugin_pixiv_dowlonder",
    "夜雨",
    "按页数直接发送 Pixiv 图片或打包 ZIP",
    "0.1.1",
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
        self._active_archives: set[Path] = set()
        self._cleanup_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None
        self._stopping = False

    async def initialize(self):
        """Start optional periodic cleanup after validating its configuration."""
        interval = self.config.get("cleanup_interval_hours", 0)
        retention = self.config.get("cleanup_retention_hours", 24)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (interval, retention)
        ):
            self.logger.warning(
                "Invalid Pixiv cleanup configuration; scheduled cleanup is disabled."
            )
            return
        if (
            interval
            and not self._stopping
            and (self._cleanup_task is None or self._cleanup_task.done())
        ):
            self._cleanup_task = asyncio.create_task(
                self._cleanup_loop(interval * 3600, retention)
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("pixiv_clean", alias={"pixiv清理"})
    async def pixiv_clean(self, event: AstrMessageEvent):
        """Delete the plugin's saved ZIPs that are not currently in use.

        Args:
            event: Administrator command event receiving cleanup statistics.
        """
        if self._stopping:
            await event.send(event.plain_result("插件正在停止，请稍后重试。"))
            return
        try:
            removed, freed, skipped, failed = await self._cleanup_archives()
        except (OSError, RuntimeError) as exc:
            self.logger.warning("Pixiv cleanup failed (%s).", type(exc).__name__)
            await event.send(
                event.plain_result("清理失败：无法访问插件文件目录，请检查权限。")
            )
            return
        await event.send(
            event.plain_result(
                f"清理完成：删除 {removed} 个 ZIP，释放 {freed / (1024 * 1024):.2f} MiB，跳过 {skipped} 个正在使用或非普通文件，失败 {failed} 个。"
            )
        )

    async def _cleanup_archives(
        self, retention_hours: int = 0
    ) -> tuple[int, int, int, int]:
        """Remove only generated ZIP files within the plugin data directory.

        Args:
            retention_hours: Minimum file age in hours; zero removes all ages.

        Returns:
            Counts of deleted files, freed bytes, skipped files, and failures.

        Raises:
            OSError: The plugin data directory cannot be accessed.
            RuntimeError: AstrBot cannot resolve or create the data directory.
        """
        removed = freed = skipped = failed = 0
        cutoff = time.time() - retention_hours * 3600
        async with self._cleanup_lock:
            data_dir = StarTools.get_data_dir(
                "astrbot_plugin_pixiv_dowlonder"
            ).resolve()
            for path in data_dir.iterdir():
                # Yield between files so downloads and uploads remain responsive.
                await asyncio.sleep(0)
                if self._stopping:
                    break
                if not re.fullmatch(
                    r"[0-9]{1,20}_[0-9]{8}_[0-9]{6}_[0-9a-f]{8}\.zip", path.name
                ):
                    continue
                if path in self._active_archives:
                    skipped += 1
                    continue
                try:
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or (
                        getattr(info, "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                    ):
                        skipped += 1
                        continue
                    if retention_hours and info.st_mtime > cutoff:
                        skipped += 1
                        continue
                    # Do not recurse or follow links outside this directory.
                    path.unlink()
                    removed += 1
                    freed += info.st_size
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    failed += 1
                    self.logger.warning(
                        "Unable to remove Pixiv archive %s (%s).",
                        path.name,
                        type(exc).__name__,
                    )
        return removed, freed, skipped, failed

    async def _cleanup_loop(self, interval_seconds: int, retention_hours: int):
        """Run retention cleanup at a fixed interval until plugin termination.

        Args:
            interval_seconds: Delay before each cleanup, including the first.
            retention_hours: Minimum age of archives eligible for deletion.
        """
        while not self._stopping:
            await asyncio.sleep(interval_seconds)
            if self._stopping:
                break
            try:
                removed, freed, skipped, failed = await self._cleanup_archives(
                    retention_hours
                )
                self.logger.info(
                    "Pixiv scheduled cleanup removed %s files, freed %s bytes, skipped %s files, and failed on %s files.",
                    removed,
                    freed,
                    skipped,
                    failed,
                )
            except Exception as exc:
                self.logger.warning(
                    "Pixiv scheduled cleanup failed (%s).", type(exc).__name__
                )

    @filter.command("pixiv")
    async def pixiv(self, event: AstrMessageEvent, pid: str = ""):
        """Download all artwork pages at the configured image size.

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
            zip_password = self.config.get("zip_password", "")
            if not isinstance(zip_password, str):
                raise PixivDownloadError("ZIP 解压密码必须是字符串，请检查插件配置。")
            image_size = self.config.get("image_size", "original")
            if not isinstance(image_size, str) or image_size not in IMAGE_SIZES:
                raise PixivDownloadError("图片尺寸必须选择原图、1200 像素或 540 像素。")
            image_label = IMAGE_SIZES[image_size][1]
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
                        "代理地址应为 HTTP(S) 地址，可含路径前缀，不能含账号、查询参数或片段。"
                    )
            cookie = str(self.config.get("pixiv_cookie", "")).strip()
            if "\r" in cookie or "\n" in cookie:
                raise PixivDownloadError("Cookie 不能包含换行，请检查插件配置。")

            temp_root = Path(get_astrbot_temp_path())
            temp_root.mkdir(parents=True, exist_ok=True)
            await event.send(
                event.plain_result(
                    f"开始下载 Pixiv 作品 {pid} 的全部{image_label}，请稍候。"
                )
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
                        "User-Agent": "Mozilla/5.0 (compatible; AstrBotPixiv/0.1.1)"
                    },
                ) as session:
                    page_count = await self._download(
                        session,
                        pid,
                        images,
                        cookie,
                        proxy,
                        int(limit * 1024 * 1024),
                        image_size,
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
                            f"作品 {pid} 下载完成，共 {page_count} 张{image_label}，已直接发送。"
                        )
                    )
                    return

                data_dir = StarTools.get_data_dir("astrbot_plugin_pixiv_dowlonder")
                # Await the worker even on cancellation before removing its input files.
                packing = asyncio.create_task(
                    asyncio.to_thread(
                        self._pack_archive,
                        images,
                        work_dir / "archive",
                        zip_password,
                    )
                )
                try:
                    packed = await asyncio.shield(packing)
                except asyncio.CancelledError:
                    await packing
                    raise
                except Exception as exc:
                    if zip_password:
                        self.logger.warning(
                            "Pixiv ZIP encryption failed for PID %s (%s).",
                            pid,
                            type(exc).__name__,
                        )
                        raise PixivDownloadError(
                            "加密 ZIP 生成失败，未发送文件，请检查插件依赖、磁盘空间及目录权限。"
                        ) from exc
                    raise
                name = f"{pid}_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.zip"
                archive = data_dir / name
                self._active_archives.add(archive)
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
            zip_label = "加密 ZIP" if zip_password else "ZIP"
            await event.send(
                event.plain_result(
                    f"作品 {pid} 下载完成，共 {page_count} 张{image_label}。{zip_label} 已发送并保留在本地。"
                )
            )
        except PixivDownloadError as exc:
            await event.send(event.plain_result(f"下载失败：{exc}"))
        except asyncio.TimeoutError:
            await event.send(
                event.plain_result(
                    "下载失败：请求超时，请检查网络或 Worker 代理，或调大请求超时。"
                )
            )
        except aiohttp.ClientError as exc:
            self.logger.warning(
                "Pixiv network failure for PID %s (%s).", pid, type(exc).__name__
            )
            await event.send(
                event.plain_result(
                    "下载失败：网络请求异常，请检查配置的 Worker 代理；未配置代理时请检查 Pixiv 连通性。"
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
            if archive is not None:
                self._active_archives.discard(archive)
            self._tasks.discard(task)

    @staticmethod
    def _pack_archive(images: Path, base_name: Path, password: str) -> str:
        """Create a plain or encrypted ZIP within the synchronous worker.

        Args:
            images: Task directory containing the selected image versions.
            base_name: Archive path without the ZIP suffix.
            password: Exact extraction password; empty disables encryption.

        Returns:
            Path to the complete archive, ready to move and send.

        Raises:
            OSError: An input or output file cannot be accessed.
            RuntimeError: The encryption library cannot create the archive.
        """
        if not password:
            return shutil.make_archive(str(base_name), "zip", root_dir=images)
        archive_path = f"{base_name}.zip"
        with pyzipper.AESZipFile(
            archive_path, "w", compression=pyzipper.ZIP_DEFLATED
        ) as archive:
            archive.setpassword(password.encode("utf-8"))
            archive.setencryption(pyzipper.WZ_AES, nbits=256)
            for image in sorted(
                images.iterdir(), key=lambda path: int(path.stem.rsplit("_p", 1)[1])
            ):
                archive.write(str(image), arcname=image.name)
        return archive_path

    async def _download(
        self,
        session: aiohttp.ClientSession,
        pid: str,
        images: Path,
        cookie: str,
        proxy: str,
        limit: int,
        image_size: str = "original",
    ) -> int:
        """Resolve artwork pages and stream the selected versions into a directory.

        Args:
            session: HTTP session without shared authentication headers.
            pid: Validated numeric artwork ID.
            images: Empty directory belonging to this request.
            cookie: Optional login cookie for AJAX requests, including via the proxy.
            proxy: Optional base URL for all Pixiv requests, including any path prefix.
            limit: Maximum cumulative number of downloaded image bytes.
            image_size: Original, 1200-pixel, or 540-pixel version to request.

        Returns:
            Number of successfully downloaded pages.

        Raises:
            PixivDownloadError: The work is inaccessible or an image is invalid.
            aiohttp.ClientError: An HTTP transport error occurs.
        """
        headers = {"Referer": "https://www.pixiv.net/"}
        url_key, image_label = IMAGE_SIZES[image_size]
        if cookie:
            headers["Cookie"] = cookie
        pages = []
        api_base = proxy or "https://www.pixiv.net"
        for suffix in ("", "/pages"):
            async with session.get(
                f"{api_base}/ajax/illust/{pid}{suffix}",
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
            image_url = urls.get(url_key) if isinstance(urls, dict) else None
            if not isinstance(image_url, str):
                raise PixivDownloadError(
                    f"Pixiv {image_label}地址缺失，请尝试其他图片尺寸。"
                )
            parsed = urlsplit(image_url)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "i.pximg.net"
                or Path(parsed.path).suffix.lower()
                not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
            ):
                raise PixivDownloadError(
                    f"Pixiv 返回了不支持的{image_label}地址或格式。"
                )
            if proxy:
                image_url = proxy + urlunsplit(("", "", parsed.path, parsed.query, ""))
            path = images / f"{pid}_p{index}{Path(parsed.path).suffix.lower()}"
            async with session.get(
                image_url,
                headers={"Referer": "https://www.pixiv.net/"},
                allow_redirects=False,
            ) as response:
                if response.status == 429:
                    raise PixivDownloadError("图片服务器请求过于频繁，请稍后重试。")
                if response.status == 403:
                    raise PixivDownloadError(
                        "图片服务器拒绝访问，请检查 Referer 或图片代理配置。"
                    )
                if 300 <= response.status < 400:
                    raise PixivDownloadError(
                        "图片请求发生跳转，请检查 Worker／镜像是否直接返回图片。"
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
                    raise PixivDownloadError("作品图片总大小超过配置上限，未生成 ZIP。")
                size = 0
                with path.open("wb") as output:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        size += len(chunk)
                        if total > limit:
                            raise PixivDownloadError(
                                "作品图片总大小超过配置上限，未生成 ZIP。"
                            )
                        output.write(chunk)
                if not size:
                    raise PixivDownloadError(f"第 {index + 1} 张图片为空，未生成 ZIP。")
        return len(pages)

    async def terminate(self):
        """Cancel active downloads and wait for temporary-file cleanup."""
        self._stopping = True
        tasks = list(self._tasks)
        if self._cleanup_task is not None:
            tasks.append(self._cleanup_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._cleanup_lock:
            pass
