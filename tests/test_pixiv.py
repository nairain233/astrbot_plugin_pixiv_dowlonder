import asyncio
import base64
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit
from zipfile import ZipFile

import aiohttp
import botpy.message
import pytest
import pytest_asyncio
import pyzipper
from aiohttp import web

from astrbot.api.event import MessageChain, MessageEventResult
from astrbot.api.message_components import File, Image
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    PlatformMetadata,
)
from astrbot.core.message import components
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
    QQOfficialMessageEvent,
)
from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_event import (
    QQOfficialWebhookMessageEvent,
)
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star_handler import star_handlers_registry
from data.plugins.astrbot_plugin_pixiv_dowlond import main as plugin_module

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII="
)


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    """Create an isolated plugin with real configuration and scratch directories.

    Args:
        tmp_path: Per-test filesystem root.
        monkeypatch: Pytest replacement manager.

    Returns:
        Plugin instance whose files cannot affect runtime data.
    """
    data = tmp_path / "saved"
    data.mkdir()
    monkeypatch.setattr(plugin_module.StarTools, "get_data_dir", lambda name: data)
    monkeypatch.setattr(
        plugin_module, "get_astrbot_temp_path", lambda: str(tmp_path / "temp")
    )
    instance = plugin_module.PixivDownloader(MagicMock(), {"direct_send_max_pages": 0})
    instance.logger = MagicMock()
    return instance


@pytest.fixture
def event():
    """Create a message event collecting actual AstrBot message components.

    Returns:
        Mock OneBot event with asynchronous message delivery.
    """
    message = MagicMock()
    message.get_platform_name.return_value = "aiocqhttp"
    message.plain_result.side_effect = lambda text: MessageEventResult().message(text)
    message.chain_result.side_effect = lambda chain: MessageEventResult(chain=chain)
    message.send = AsyncMock()
    return message


@pytest_asyncio.fixture
async def http(monkeypatch):
    """Route production HTTP requests into a local aiohttp server.

    Args:
        monkeypatch: Pytest replacement manager.

    Yields:
        Mutable routes and a log of original URLs and request options.
    """
    state = SimpleNamespace(
        routes={
            "/ajax/illust/123": web.json_response(
                {"error": False, "body": {"illustType": 0}}
            ),
            "/ajax/illust/123/pages": web.json_response(
                {
                    "error": False,
                    "body": [
                        {"urls": {"original": "https://i.pximg.net/images/123_p0.png"}}
                    ],
                }
            ),
            "/images/123_p0.png": web.Response(body=PNG, content_type="image/png"),
        },
        requests=[],
        received=[],
        allowed_hosts=None,
    )

    async def handle(request):
        """Serve a configured response or asynchronous streaming handler.

        Args:
            request: Local test HTTP request.

        Returns:
            Configured response or a not-found response.
        """
        state.received.append(
            (request.path, dict(request.query), dict(request.headers))
        )
        response = state.routes.get(request.path, web.Response(status=404))
        if callable(response):
            return await response(request)
        return response

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    original_request = aiohttp.ClientSession._request

    async def route(session, method, url, **kwargs):
        """Preserve request options while redirecting transport to the test server.

        Args:
            session: Real production ClientSession instance.
            method: HTTP method.
            url: Original upstream URL.
            **kwargs: Unmodified aiohttp request options.

        Returns:
            Real aiohttp response from the local server.
        """
        state.requests.append((str(url), kwargs, session.cookie_jar))
        parsed = urlsplit(str(url))
        if state.allowed_hosts is not None:
            assert parsed.hostname in state.allowed_hosts
        local_url = f"http://127.0.0.1:{port}{parsed.path}"
        if parsed.query:
            local_url += f"?{parsed.query}"
        return await original_request(session, method, local_url, **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "_request", route)
    state.base_url = f"http://127.0.0.1:{port}"
    try:
        yield state
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artwork_proxy", "image_proxy", "api_base", "image_base"),
    [
        ("", "", "https://www.pixiv.net", "https://i.pximg.net"),
        ("api.example.com", "", "https://api.example.com", "https://i.pximg.net"),
        (
            "",
            "images.example.com",
            "https://www.pixiv.net",
            "https://images.example.com",
        ),
        (
            " api.example.com:8443/pixiv/ ",
            " images.example.com:9443/pximg/ ",
            "https://api.example.com:8443/pixiv",
            "https://images.example.com:9443/pximg",
        ),
        (
            "proxy.example.com/bridge",
            "proxy.example.com/bridge",
            "https://proxy.example.com/bridge",
            "https://proxy.example.com/bridge",
        ),
        (
            "https://api.example.com/meta/",
            "https://images.example.com/pximg/",
            "https://api.example.com/meta",
            "https://images.example.com/pximg",
        ),
        (
            "http://api.example.com:8080/meta/",
            "http://images.example.com:8081/pximg/",
            "http://api.example.com:8080/meta",
            "http://images.example.com:8081/pximg",
        ),
        (
            "",
            "https://images.example.com/pximg/",
            "https://www.pixiv.net",
            "https://images.example.com/pximg",
        ),
    ],
)
@pytest.mark.parametrize("cookie", ["", "PHPSESSID=test-secret"])
async def test_complete_archive_and_cookie_isolation(
    plugin,
    event,
    http,
    tmp_path,
    artwork_proxy,
    image_proxy,
    api_base,
    image_base,
    cookie,
):
    plugin.config.update(
        {
            "pixiv_cookie": cookie,
            "artwork_proxy_base_url": artwork_proxy,
            "image_proxy_base_url": image_proxy,
        }
    )
    original_config = dict(plugin.config)
    api_prefix = urlsplit(api_base).path
    image_prefix = urlsplit(image_base).path
    http.routes[api_prefix + "/ajax/illust/123"] = web.json_response(
        {"error": False, "body": {"illustType": 0}},
        headers={"Set-Cookie": "unexpected=artwork-secret"},
    )
    http.routes[api_prefix + "/ajax/illust/123/pages"] = web.json_response(
        {
            "error": False,
            "body": [
                {
                    "urls": {
                        "original": "https://i.pximg.net/images/123_p0.png?token=a%2Fb"
                    }
                },
                {"urls": {"original": "https://i.pximg.net/images/123_p1.jpg"}},
            ],
        }
    )
    http.routes[f"{image_prefix}/images/123_p0.png"] = web.Response(
        body=PNG, content_type="image/png", headers={"Set-Cookie": "unexpected=secret"}
    )
    http.routes[f"{image_prefix}/images/123_p1.jpg"] = web.Response(
        body=b"second-original", content_type="image/jpeg"
    )
    http.allowed_hosts = {urlsplit(api_base).hostname, urlsplit(image_base).hostname}
    await plugin.pixiv(event, "00123")

    archives = list((tmp_path / "saved").glob("*.zip"))
    assert len(archives) == 1
    with ZipFile(archives[0]) as archive:
        assert archive.namelist() == ["123_p0.png", "123_p1.jpg"]
        assert archive.read("123_p0.png") == PNG
        assert archive.read("123_p1.jpg") == b"second-original"
    attachments = [
        c
        for call in event.send.call_args_list
        for c in call.args[0].chain
        if isinstance(c, File)
    ]
    assert len(attachments) == 1
    assert attachments[0].file == str(archives[0])
    assert "2 张" in event.send.call_args.args[0].chain[0].text
    assert not list((tmp_path / "temp").iterdir())
    assert not plugin._tasks
    assert len(http.requests) == 4
    for index, (_, options, jar) in enumerate(http.requests):
        expected_referer = (
            "https://www.pixiv.net/artworks/123"
            if index < 2
            else "https://www.pixiv.net/"
        )
        assert options["headers"]["Referer"] == expected_referer
        assert http.received[index][2]["Referer"] == expected_referer
        assert "Firefox/" in http.received[index][2]["User-Agent"]
        assert (
            http.received[index][2]["Accept-Language"] == "zh-CN,zh;q=0.8,zh-TW;q=0.2"
        )
        assert isinstance(jar, aiohttp.DummyCookieJar)
        assert options["allow_redirects"] is False
        if index < 2 and cookie:
            assert options["headers"]["Cookie"] == cookie
            assert http.received[index][2]["Cookie"] == cookie
        else:
            assert "Cookie" not in options["headers"]
            assert "Cookie" not in http.received[index][2]
    assert http.requests[0][0] == api_base + "/ajax/illust/123"
    assert http.requests[1][0] == api_base + "/ajax/illust/123/pages"
    assert http.received[0][1] == {}
    assert http.received[1][1] == {"lang": "zh"}
    assert http.received[0][2]["Accept"] == "application/json, text/plain, */*"
    assert http.received[1][2]["Accept"] == "application/json, text/plain, */*"
    assert http.requests[2][0] == image_base + "/images/123_p0.png?token=a%2Fb"
    assert http.requests[3][0] == image_base + "/images/123_p1.jpg"
    assert plugin.config == original_config
    assert "test-secret" not in str(plugin.logger.mock_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_mode", [False, True], ids=["default", "enabled"])
@pytest.mark.parametrize("rating", [1, 2], ids=["r18", "r18g"])
@pytest.mark.parametrize("artwork_proxy", ["", "api.example.com/meta"])
@pytest.mark.parametrize("delivery", ["direct", "zip", "encrypted"])
async def test_healthy_mode_blocks_before_pages_and_images(
    plugin,
    event,
    http,
    tmp_path,
    monkeypatch,
    explicit_mode,
    rating,
    artwork_proxy,
    delivery,
):
    """Block restricted works before any image or archive processing.

    Args:
        plugin: Isolated downloader instance.
        event: Message event collecting outbound messages.
        http: Local HTTP routes and request log.
        tmp_path: Per-test filesystem root.
        monkeypatch: Pytest replacement manager.
        explicit_mode: Whether to set the enabled flag explicitly.
        rating: Pixiv adult rating to block.
        artwork_proxy: Optional artwork proxy with a path prefix.
        delivery: Configured image or archive delivery mode.
    """
    plugin.config.update(
        {
            "artwork_proxy_base_url": artwork_proxy,
            "image_proxy_base_url": "images.example.com/pximg",
            "pixiv_cookie": "PHPSESSID=test-secret",
            "direct_send_max_pages": 3 if delivery == "direct" else 0,
            "zip_password": "test-password" if delivery == "encrypted" else "",
        }
    )
    if explicit_mode:
        plugin.config["healthy_mode"] = True
    else:
        assert "healthy_mode" not in plugin.config
    api_base = (
        "https://api.example.com/meta" if artwork_proxy else "https://www.pixiv.net"
    )
    prefix = "/meta" if artwork_proxy else ""
    http.routes[prefix + "/ajax/illust/123"] = web.json_response(
        {"body": {"illustType": 0, "xRestrict": rating}}
    )
    pack = MagicMock()
    download = AsyncMock()
    monkeypatch.setattr(plugin, "_pack_archive", pack)
    monkeypatch.setattr(plugin, "_download", download)

    await plugin.pixiv(event, "123")

    rating_label = "R-18" if rating == 1 else "R-18G"
    assert f"健康模式已开启，该作品属于 {rating_label}，已停止下载和发送。" in (
        event.send.call_args.args[0].chain[0].text
    )
    assert [url for url, _, _ in http.requests] == [api_base + "/ajax/illust/123"]
    assert not any(
        isinstance(component, (File, Image))
        for call in event.send.call_args_list
        for component in call.args[0].chain
    )
    download.assert_not_awaited()
    pack.assert_not_called()
    plugin.logger.info.assert_called_once_with(
        "Pixiv healthy mode blocked PID %s rating=%s route=%s.",
        "123",
        rating_label,
        "proxy" if artwork_proxy else "direct",
    )
    assert "test-secret" not in str(plugin.logger.mock_calls)
    assert "test-password" not in str(plugin.logger.mock_calls)
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())
    assert not plugin._tasks
    assert not plugin._active_archives


@pytest.mark.asyncio
@pytest.mark.parametrize("artwork_proxy", ["", "api.example.com/meta"])
@pytest.mark.parametrize(
    "rating_data",
    [
        pytest.param({}, id="missing"),
        *[
            pytest.param({"xRestrict": rating}, id=name)
            for name, rating in (
                ("safe", 0),
                ("null", None),
                ("negative", -1),
                ("unknown", 3),
                ("string-r18", "1"),
                ("string-r18g", "2"),
                ("true", True),
                ("false", False),
                ("float-r18", 1.0),
                ("float-r18g", 2.0),
                ("list", [1]),
                ("dict", {"value": 1}),
            )
        ],
    ],
)
async def test_healthy_mode_allows_safe_or_unknown_rating(
    plugin, event, http, tmp_path, artwork_proxy, rating_data
):
    """Allow safe or unrecognized metadata without treating it as an adult rating.

    Args:
        plugin: Isolated downloader instance.
        event: Message event collecting outbound messages.
        http: Local HTTP routes and request log.
        tmp_path: Per-test filesystem root.
        artwork_proxy: Optional artwork proxy with a path prefix.
        rating_data: Safe, missing, or invalid rating metadata.
    """
    plugin.config.update(
        {"healthy_mode": True, "artwork_proxy_base_url": artwork_proxy}
    )
    prefix = "/meta" if artwork_proxy else ""
    http.routes[prefix + "/ajax/illust/123"] = web.json_response(
        {"body": {"illustType": 0, **rating_data}}
    )
    http.routes[prefix + "/ajax/illust/123/pages"] = http.routes[
        "/ajax/illust/123/pages"
    ]

    await plugin.pixiv(event, "123")

    assert len(http.requests) == 3
    archive_path = next((tmp_path / "saved").glob("*.zip"))
    with ZipFile(archive_path) as archive:
        assert archive.read("123_p0.png") == PNG
    assert any(
        isinstance(component, File)
        for call in event.send.call_args_list
        for component in call.args[0].chain
    )
    plugin.logger.info.assert_not_called()
    assert not list((tmp_path / "temp").iterdir())
    assert not plugin._tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("rating", [1, 2])
@pytest.mark.parametrize("artwork_proxy", ["", "api.example.com/meta"])
@pytest.mark.parametrize("delivery", ["direct", "zip", "encrypted"])
async def test_disabled_healthy_mode_preserves_delivery(
    plugin, event, http, tmp_path, rating, artwork_proxy, delivery
):
    """Deliver adult-rated artwork normally when healthy mode is disabled.

    Args:
        plugin: Isolated downloader instance.
        event: Message event collecting outbound messages.
        http: Local HTTP routes and request log.
        tmp_path: Per-test filesystem root.
        rating: Pixiv adult rating allowed while filtering is disabled.
        artwork_proxy: Optional artwork proxy with a path prefix.
        delivery: Configured image or archive delivery mode.
    """
    plugin.config.update(
        {
            "healthy_mode": False,
            "artwork_proxy_base_url": artwork_proxy,
            "direct_send_max_pages": 3 if delivery == "direct" else 0,
            "zip_password": "test-password" if delivery == "encrypted" else "",
        }
    )
    prefix = "/meta" if artwork_proxy else ""
    http.routes[prefix + "/ajax/illust/123"] = web.json_response(
        {"body": {"illustType": 0, "xRestrict": rating}}
    )
    http.routes[prefix + "/ajax/illust/123/pages"] = http.routes[
        "/ajax/illust/123/pages"
    ]
    delivered = []

    async def receive(message):
        """Capture directly sent image bytes before temporary files are removed.

        Args:
            message: Outbound message chain.
        """
        for component in message.chain:
            if isinstance(component, Image):
                delivered.append(Path(component.path).read_bytes())

    event.send.side_effect = receive
    await plugin.pixiv(event, "123")

    assert len(http.requests) == 3
    archives = list((tmp_path / "saved").glob("*.zip"))
    if delivery == "direct":
        assert delivered == [PNG]
        assert not archives
    else:
        assert not delivered
        assert len(archives) == 1
        archive_type = pyzipper.AESZipFile if delivery == "encrypted" else ZipFile
        password = b"test-password" if delivery == "encrypted" else None
        with archive_type(str(archives[0])) as archive:
            assert archive.read("123_p0.png", pwd=password) == PNG
        assert any(
            isinstance(component, File)
            for call in event.send.call_args_list
            for component in call.args[0].chain
        )
    plugin.logger.info.assert_not_called()
    assert not list((tmp_path / "temp").iterdir())
    assert not plugin._tasks
    assert not plugin._active_archives


@pytest.mark.asyncio
@pytest.mark.parametrize("artwork_proxy", ["", "api.example.com/meta"])
@pytest.mark.parametrize("status", [302, 502])
@pytest.mark.parametrize(
    ("path", "request_count"),
    [("/ajax/illust/123", 1), ("/ajax/illust/123/pages", 2), ("/images/123_p0.png", 3)],
)
async def test_download_failure_never_redirects_or_switches_routes(
    plugin, event, http, tmp_path, artwork_proxy, status, path, request_count
):
    """Keep each stage on its configured route when a download step fails.

    Args:
        plugin: Isolated downloader instance.
        event: Message event collecting responses.
        http: Local HTTP routes and request log.
        tmp_path: Per-test filesystem root.
        artwork_proxy: Optional independent artwork proxy address.
        status: Proxy failure or redirect response status.
        path: Artwork or image endpoint that fails.
        request_count: Number of requests before stopping.
    """
    plugin.config["image_proxy_base_url"] = "https://images.example.com/pximg"
    plugin.config["artwork_proxy_base_url"] = artwork_proxy
    api_base = (
        "https://api.example.com/meta" if artwork_proxy else "https://www.pixiv.net"
    )
    api_prefix = "/meta" if artwork_proxy else ""
    http.allowed_hosts = {urlsplit(api_base).hostname, "images.example.com"}
    redirected = []

    async def redirect_target(request):
        """Record any incorrectly followed redirect.

        Args:
            request: Unexpected redirected HTTP request.

        Returns:
            A response that must never be requested.
        """
        redirected.append(request.path)
        return web.Response(body=PNG, content_type="image/png")

    http.routes[path] = web.Response(
        status=status, headers={"Location": http.base_url + "/redirected"}
    )
    http.routes = {
        ("/pximg" if path.startswith("/images/") else api_prefix) + path: response
        for path, response in http.routes.items()
    }
    http.routes["/redirected"] = redirect_target
    await plugin.pixiv(event, "123")
    message = event.send.call_args.args[0].chain[0].text
    assert "跳转" in message if status == 302 else "HTTP 502" in message
    assert len(http.requests) == request_count
    assert [url for url, _, _ in http.requests] == [
        api_base + "/ajax/illust/123",
        api_base + "/ajax/illust/123/pages",
        "https://images.example.com/pximg/images/123_p0.png",
    ][:request_count]
    assert all(options["allow_redirects"] is False for _, options, _ in http.requests)
    assert not redirected
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("artwork_proxy", ["", "api.example.com/meta"])
@pytest.mark.parametrize("proxy", ["", "https://images.example.com/pximg"])
@pytest.mark.parametrize("suffix", ["", "/pages"])
@pytest.mark.parametrize("content_type", ["application/json", "text/html"])
async def test_artwork_forbidden_reports_stage_without_logging_secrets(
    plugin, event, http, tmp_path, artwork_proxy, proxy, suffix, content_type
):
    """Distinguish artwork access failures while keeping credentials out of logs.

    Args:
        plugin: Isolated downloader instance.
        event: Message event collecting responses.
        http: Local HTTP routes and request log.
        tmp_path: Per-test filesystem root.
        artwork_proxy: Optional independent artwork proxy address.
        proxy: Optional Worker URL with a path prefix.
        suffix: Artwork details or pages endpoint suffix.
        content_type: JSON error or HTML block page returned by the endpoint.
    """
    plugin.config.update(
        {
            "artwork_proxy_base_url": artwork_proxy,
            "image_proxy_base_url": proxy,
            "pixiv_cookie": "PHPSESSID=request-secret",
        }
    )
    http.routes[f"/ajax/illust/123{suffix}"] = web.Response(
        status=403,
        text='{"message": "response-secret"}',
        content_type=content_type,
    )
    api_prefix = "/meta" if artwork_proxy else ""
    if artwork_proxy:
        http.routes = {
            api_prefix + path: response for path, response in http.routes.items()
        }
    http.allowed_hosts = {"api.example.com" if artwork_proxy else "www.pixiv.net"}
    await plugin.pixiv(event, "123")

    message = event.send.call_args.args[0].chain[0].text
    assert "作品信息接口拒绝访问" in message
    assert "HTTP 403" in message
    assert ("非 JSON" in message) == (content_type != "application/json")
    assert ("反代" if artwork_proxy else "直连") in message
    assert "Worker 代理" not in message
    assert "Cookie 是否有效" not in message
    plugin.logger.warning.assert_called_once()
    logged = plugin.logger.warning.call_args.args
    assert logged[1:] == (
        "123",
        "pages" if suffix else "details",
        "proxy" if artwork_proxy else "direct",
        403,
        content_type,
    )
    assert "request-secret" not in str(plugin.logger.mock_calls)
    assert "response-secret" not in str(plugin.logger.mock_calls)
    assert len(http.requests) == (2 if suffix else 1)
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("artwork_proxy", ["", "api.example.com/meta"])
async def test_artwork_invalid_json_reports_route(
    plugin, event, http, tmp_path, artwork_proxy
):
    """Keep malformed artwork JSON failures in the resolution stage.

    Args:
        plugin: Isolated downloader instance.
        event: Message event collecting responses.
        http: Local HTTP routes and request log.
        tmp_path: Per-test filesystem root.
        artwork_proxy: Optional independent artwork proxy address.
    """
    plugin.config.update(
        {
            "artwork_proxy_base_url": artwork_proxy,
            "image_proxy_base_url": "images.example.com",
            "pixiv_cookie": "PHPSESSID=request-secret",
        }
    )
    prefix = "/meta" if artwork_proxy else ""
    http.routes[prefix + "/ajax/illust/123"] = web.Response(
        text="invalid-json-response-secret", content_type="application/json"
    )
    http.allowed_hosts = {"api.example.com" if artwork_proxy else "www.pixiv.net"}
    await plugin.pixiv(event, "123")
    stage = "作品解析（反代）" if artwork_proxy else "作品解析（直连）"
    message = event.send.call_args.args[0].chain[0].text
    assert stage in message
    assert "响应格式无效" in message
    assert stage in str(plugin.logger.mock_calls)
    assert "request-secret" not in str(plugin.logger.mock_calls)
    assert "response-secret" not in str(plugin.logger.mock_calls)
    assert len(http.requests) == 1
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())


@pytest.mark.asyncio
async def test_single_page_anonymous_and_repeated_download(
    plugin, event, http, tmp_path
):
    await plugin.pixiv(event, "123")
    # Each HTTP response is replaced because aiohttp responses are single-use.
    http.routes["/images/123_p0.png"] = web.Response(body=PNG, content_type="image/png")
    http.routes["/ajax/illust/123"] = web.json_response({"body": {"illustType": 1}})
    http.routes["/ajax/illust/123/pages"] = web.json_response(
        {"body": [{"urls": {"original": "https://i.pximg.net/images/123_p0.png"}}]}
    )
    await plugin.pixiv(event, "123")
    assert len(list((tmp_path / "saved").glob("*.zip"))) == 2
    assert all("Cookie" not in options["headers"] for _, options, _ in http.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pid",
    ["", "0", "-1", "1.2", "１２３", "https://www.pixiv.net/artworks/123", "1" * 21],
)
async def test_bad_pid_does_not_start_download(plugin, event, http, pid):
    await plugin.pixiv(event, pid)
    assert "用法" in event.send.call_args.args[0].chain[0].text
    assert not http.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"request_timeout": 0}, "必须为正数"),
        ({"max_download_mb": -1}, "必须为正数"),
        ({"request_timeout": "nan"}, "必须为正数"),
        ({"max_download_mb": "inf"}, "必须为正数"),
        ({"request_timeout": "bad"}, "配置"),
        ({"image_proxy_base_url": "ftp://example.com"}, "代理地址"),
        ({"image_proxy_base_url": "https://user:password@example.com"}, "代理地址"),
        ({"image_proxy_base_url": "https://example.com?token=secret"}, "代理地址"),
        ({"image_proxy_base_url": "https://example.com/#fragment"}, "代理地址"),
        ({"pixiv_cookie": "foo=bar\nSecret: value"}, "Cookie 不能包含换行"),
        ({"direct_send_max_pages": -1}, "直接发送页数上限"),
        ({"direct_send_max_pages": 1.5}, "直接发送页数上限"),
        ({"direct_send_max_pages": "3"}, "直接发送页数上限"),
        ({"direct_send_max_pages": True}, "直接发送页数上限"),
        ({"zip_password": None}, "ZIP 解压密码必须是字符串"),
        ({"zip_password": 123}, "ZIP 解压密码必须是字符串"),
        ({"image_size": "720"}, "图片尺寸必须选择"),
        ({"image_size": 540}, "图片尺寸必须选择"),
        ({"image_size": None}, "图片尺寸必须选择"),
        ({"image_size": []}, "图片尺寸必须选择"),
        *[
            ({"healthy_mode": value}, "健康模式必须是布尔值")
            for value in (None, 0, 1, 1.0, "true", "false", [], {})
        ],
    ],
)
async def test_invalid_config(plugin, event, http, config, message):
    plugin.config.update(config)
    await plugin.pixiv(event, "123")
    assert message in event.send.call_args.args[0].chain[0].text
    assert not http.requests
    assert not plugin._tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["artwork_proxy_base_url", "image_proxy_base_url"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        {},
        "ftp://example.com",
        "https://",
        "http:/example.com",
        "//example.com",
        "user:password@example.com",
        "https://user:password@example.com",
        "example.com?token=secret",
        "example.com?",
        "example.com/#fragment",
        "example.com:bad",
        "example.com:65536",
        "example.com:0",
        "example.com:",
        "example .com",
        "example.com\n/path",
        "https://[::1",
    ],
)
async def test_invalid_proxy_address_stops_before_requests(
    plugin, event, http, field, value
):
    """Reject either invalid proxy address without sending requests or secrets.

    Args:
        plugin: Isolated downloader instance.
        event: Message event collecting responses.
        http: Local HTTP routes and request log.
        field: Artwork or image proxy configuration key.
        value: Invalid address or configuration type.
    """
    plugin.config[field] = value
    await plugin.pixiv(event, "123")
    message = event.send.call_args.args[0].chain[0].text
    assert (
        "作品解析代理地址" if field == "artwork_proxy_base_url" else "图片下载代理地址"
    ) in message
    assert not http.requests
    assert not plugin._tasks
    assert "password" not in message
    assert "secret" not in message
    assert "password" not in str(plugin.logger.mock_calls)
    assert "secret" not in str(plugin.logger.mock_calls)


@pytest.mark.asyncio
async def test_image_download_accepts_pre_resolved_pages(plugin, http, tmp_path):
    """Download supplied pages without accessing any artwork endpoint.

    Args:
        plugin: Isolated downloader instance.
        http: Local HTTP routes and request log.
        tmp_path: Per-test filesystem root.
    """
    images = tmp_path / "images"
    images.mkdir()
    http.allowed_hosts = {"images.example.com"}
    pages = [{"urls": {"original": "https://i.pximg.net/images/123_p0.png"}}]
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        count = await plugin._download(
            session, "123", images, pages, "https://images.example.com", 1024
        )
    assert count == 1
    assert (images / "123_p0.png").read_bytes() == PNG
    assert [url for url, _, _ in http.requests] == [
        "https://images.example.com/images/123_p0.png"
    ]
    assert "Cookie" not in http.received[0][2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "status", "payload", "message"),
    [
        ("/ajax/illust/123", 401, None, "拒绝访问"),
        ("/ajax/illust/123", 403, None, "拒绝访问"),
        ("/ajax/illust/123", 404, None, "不存在"),
        ("/ajax/illust/123", 429, None, "过于频繁"),
        ("/ajax/illust/123", 500, None, "HTTP 500"),
        ("/ajax/illust/123", 302, None, "跳转"),
        ("/ajax/illust/123", 200, {"error": True}, "不可访问"),
        ("/ajax/illust/123", 200, {"body": {"illustType": 2}}, "动图"),
        ("/ajax/illust/123", 200, {"body": {}}, "详情格式异常"),
        ("/ajax/illust/123", 200, [], "不可访问"),
        ("/ajax/illust/123/pages", 200, {"body": []}, "没有可下载"),
        ("/ajax/illust/123/pages", 200, {"body": [{}]}, "地址缺失"),
        (
            "/ajax/illust/123/pages",
            200,
            {"body": [{"urls": {"original": "https://evil.example/123.png"}}]},
            "不支持的原图地址",
        ),
        ("/images/123_p0.png", 404, None, "第 1 张"),
        ("/images/123_p0.png", 403, None, "图片服务器拒绝"),
        ("/images/123_p0.png", 429, None, "过于频繁"),
    ],
)
async def test_remote_errors_never_publish_partial_zip(
    plugin, event, http, tmp_path, path, status, payload, message
):
    http.routes[path] = (
        web.json_response(payload, status=status)
        if payload is not None
        else web.Response(status=status, headers={"Location": "https://other.example/"})
    )
    await plugin.pixiv(event, "123")
    assert message in event.send.call_args.args[0].chain[0].text
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())
    if status == 302:
        assert len(http.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/ajax/illust/123", "/images/123_p0.png"])
async def test_html_challenge_is_not_archived(plugin, event, http, tmp_path, path):
    http.routes[path] = web.Response(
        text="<html>Challenge</html>", content_type="text/html"
    )
    await plugin.pixiv(event, "123")
    assert "下载失败" in event.send.call_args.args[0].chain[0].text
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())


@pytest.mark.asyncio
async def test_empty_image(plugin, event, http, tmp_path):
    http.routes["/images/123_p0.png"] = web.Response(body=b"", content_type="image/png")
    await plugin.pixiv(event, "123")
    assert "图片为空" in event.send.call_args.args[0].chain[0].text
    assert not list((tmp_path / "saved").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_size_limit_checks_headers_and_stream(
    plugin, event, http, tmp_path, streamed
):
    plugin.config["max_download_mb"] = 0.0001

    async def stream(request):
        response = web.StreamResponse(headers={"Content-Type": "image/png"})
        await response.prepare(request)
        await response.write(b"x" * 256)
        await response.write_eof()
        return response

    http.routes["/images/123_p0.png"] = (
        stream if streamed else web.Response(body=b"x" * 256, content_type="image/png")
    )
    await plugin.pixiv(event, "123")
    assert "总大小超过" in event.send.call_args.args[0].chain[0].text
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())


@pytest.mark.asyncio
async def test_later_page_failure_discards_earlier_image(plugin, event, http, tmp_path):
    http.routes["/ajax/illust/123/pages"] = web.json_response(
        {
            "body": [
                {"urls": {"original": "https://i.pximg.net/images/123_p0.png"}},
                {"urls": {"original": "https://i.pximg.net/images/123_p1.png"}},
            ]
        }
    )
    await plugin.pixiv(event, "123")
    assert "第 2 张" in event.send.call_args.args[0].chain[0].text
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform", ["aiocqhttp", "qq_official", "qq_official_webhook"]
)
async def test_upload_failure_preserves_archive(
    plugin, event, http, tmp_path, platform
):
    event.get_platform_name.return_value = platform
    event.send.side_effect = [None, RuntimeError("upload failed"), None]
    await plugin.pixiv(event, "123")
    archive = next((tmp_path / "saved").glob("*.zip"))
    assert str(archive) in event.send.call_args.args[0].chain[0].text
    assert "发送失败" in event.send.call_args.args[0].chain[0].text
    text = event.send.call_args.args[0].chain[0].text
    assert ("NapCat" in text) is (platform == "aiocqhttp")
    if platform != "aiocqhttp":
        assert "QQ 官方 Bot" in text
    assert not list((tmp_path / "temp").iterdir())
    assert ZipFile(archive).testzip() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("proxied", [False, True])
@pytest.mark.parametrize("method", ["_resolve_pages", "_download"])
@pytest.mark.parametrize(
    "error_type", [asyncio.TimeoutError, aiohttp.ClientConnectionError]
)
async def test_stage_network_failure_reports_route(
    plugin, event, http, monkeypatch, tmp_path, proxied, method, error_type
):
    """Report the failing stage and route without fallback or exception secrets.

    Args:
        plugin: Isolated downloader instance.
        event: Message event collecting responses.
        http: Local HTTP routes and request log.
        monkeypatch: Pytest replacement manager.
        tmp_path: Per-test filesystem root.
        proxied: Whether each stage uses its independent proxy.
        method: Artwork resolution or image download stage to fail.
        error_type: Timeout or connection exception to simulate.
    """
    if proxied:
        plugin.config.update(
            {
                "artwork_proxy_base_url": "api.example.com",
                "image_proxy_base_url": "images.example.com",
            }
        )
    monkeypatch.setattr(
        plugin, method, AsyncMock(side_effect=error_type("exception-secret"))
    )
    await plugin.pixiv(event, "123")
    message = event.send.call_args.args[0].chain[0].text
    stage = "作品解析" if method == "_resolve_pages" else "图片下载"
    route = "反代" if proxied else "直连"
    assert f"{stage}（{route}）" in message
    assert (
        "请求超时" if error_type is asyncio.TimeoutError else "网络请求异常"
    ) in message
    assert len(http.requests) == (0 if method == "_resolve_pages" else 2)
    assert f"{stage}（{route}）" in str(plugin.logger.mock_calls)
    assert "exception-secret" not in str(plugin.logger.mock_calls)
    assert "exception-secret" not in message
    assert not list((tmp_path / "temp").iterdir())
    assert not plugin._tasks


@pytest.mark.asyncio
async def test_parallel_tasks_and_termination(
    plugin, event, http, monkeypatch, tmp_path
):
    entered = asyncio.Queue()

    async def block(*args):
        await entered.put(True)
        await asyncio.Event().wait()

    monkeypatch.setattr(plugin, "_download", block)
    monkeypatch.setattr(plugin, "_resolve_pages", AsyncMock(return_value=[{}]))
    first = asyncio.create_task(plugin.pixiv(event, "123"))
    second = asyncio.create_task(plugin.pixiv(event, "456"))
    await asyncio.wait_for(entered.get(), 2)
    await asyncio.wait_for(entered.get(), 2)
    assert len(plugin._tasks) == 2
    assert len(list((tmp_path / "temp").iterdir())) == 2
    await plugin.pixiv(event, "789")
    assert "繁忙" in event.send.call_args.args[0].chain[0].text
    await plugin.terminate()
    assert first.cancelled() and second.cancelled()
    assert not plugin._tasks
    assert not list((tmp_path / "temp").iterdir())
    assert not list((tmp_path / "saved").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("password", ["", "test-zip-password"])
async def test_cancel_during_zip_waits_for_worker_cleanup(
    plugin, event, http, monkeypatch, tmp_path, password
):
    plugin.config["zip_password"] = password
    started = threading.Event()
    release = threading.Event()
    original = plugin._pack_archive

    def pack(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(plugin, "_pack_archive", pack)
    task = asyncio.create_task(plugin.pixiv(event, "123"))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert list((tmp_path / "temp").glob("pixiv_*/images/123_p0.png"))
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert not list((tmp_path / "temp").iterdir())
    assert not list((tmp_path / "saved").iterdir())


@pytest.mark.asyncio
async def test_unsupported_platform(plugin, event, http):
    event.get_platform_name.return_value = "telegram"
    await plugin.pixiv(event, "123")
    assert "仅支持 QQ" in event.send.call_args.args[0].chain[0].text
    assert not http.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "event_class"),
    [
        ("qq_official", QQOfficialMessageEvent),
        ("qq_official_webhook", QQOfficialWebhookMessageEvent),
    ],
)
@pytest.mark.parametrize("is_group", [False, True])
@pytest.mark.parametrize("delivery", ["direct", "zip", "encrypted"])
async def test_qqofficial_delivery(
    plugin, http, tmp_path, monkeypatch, platform, event_class, is_group, delivery
):
    """Send local images and archives through real QQ Official event adapters.

    Args:
        plugin: Isolated downloader instance.
        http: Local Pixiv and image service.
        tmp_path: Per-test filesystem root.
        monkeypatch: Pytest replacement manager.
        platform: Official QQ adapter name.
        event_class: WebSocket or Webhook message event class.
        is_group: Whether to reply in a QQ group rather than C2C.
        delivery: Direct images, plain ZIP, or encrypted ZIP.
    """
    plugin.config["direct_send_max_pages"] = 2 if delivery == "direct" else 0
    if delivery == "encrypted":
        plugin.config["zip_password"] = "qq-test-password"
    http.routes["/ajax/illust/123/pages"] = web.json_response(
        {
            "body": [
                {"urls": {"original": f"https://i.pximg.net/images/123_p{index}.png"}}
                for index in range(2)
            ]
        }
    )
    http.routes["/images/123_p1.png"] = web.Response(body=PNG, content_type="image/png")
    raw_class = botpy.message.GroupMessage if is_group else botpy.message.C2CMessage
    raw = raw_class(
        api=None,
        event_id="event-1",
        data={
            "id": "msg-1",
            "author": {"member_openid": "user-1", "user_openid": "user-1"},
            "group_openid": "group-1",
            "content": "/pixiv 123",
            "timestamp": "0",
        },
    )
    message = AstrBotMessage()
    message.message_id = "msg-1"
    message.session_id = "group-1" if is_group else "user-1"
    message.group_id = "group-1" if is_group else ""
    message.self_id = "bot-1"
    message.sender = MessageMember(user_id="user-1", nickname="tester")
    message.type = MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE
    message.message_str = "/pixiv 123"
    message.message = []
    message.raw_message = raw
    sender = AsyncMock(return_value={"id": "reply-1"})
    bot = SimpleNamespace(api=SimpleNamespace(post_group_message=sender))
    event = event_class(
        message.message_str,
        message,
        PlatformMetadata(name=platform, description="QQ Official", id="qq-test"),
        message.session_id,
        bot,
    )
    event.post_c2c_message = sender
    monkeypatch.setattr(AstrMessageEvent, "send", AsyncMock())
    uploaded = []

    async def upload(source, file_type, **kwargs):
        """Read media before the plugin removes temporary source files.

        Args:
            source: Base64 image or local archive path.
            file_type: QQ media type.
            **kwargs: Upload target and optional filename.

        Returns:
            Mock QQ upload response used by the real message sender.
        """
        assert kwargs["group_openid" if is_group else "openid"] == message.session_id
        if file_type == 1:
            assert base64.b64decode(source) == PNG
        else:
            assert file_type == 4
            archive = Path(source)
            assert archive.is_file()
            assert kwargs["file_name"] == archive.name
            with pyzipper.AESZipFile(archive) as packed:
                if delivery == "encrypted":
                    assert all(info.flag_bits & 1 for info in packed.infolist())
                    packed.setpassword(b"qq-test-password")
                assert packed.namelist() == ["123_p0.png", "123_p1.png"]
                assert packed.read("123_p0.png") == PNG
                assert packed.read("123_p1.png") == PNG
        uploaded.append(file_type)
        return {"file_info": "uploaded-media"}

    event.upload_group_and_c2c_image = AsyncMock(side_effect=upload)
    event.upload_group_and_c2c_media = AsyncMock(side_effect=upload)
    await plugin.pixiv(event, "123")

    assert uploaded == ([1, 1] if delivery == "direct" else [4])
    calls = [call.kwargs for call in sender.await_args_list]
    assert len(calls) == (4 if delivery == "direct" else 3)
    assert all(call["msg_id"] == "msg-1" for call in calls)
    assert all(
        call["group_openid" if is_group else "openid"] == message.session_id
        for call in calls
    )
    assert all(call["msg_type"] == 7 for call in calls[1:-1])
    assert all(call["media"] == {"file_info": "uploaded-media"} for call in calls[1:-1])
    assert not list((tmp_path / "temp").iterdir())
    assert len(list((tmp_path / "saved").glob("*.zip"))) == (delivery != "direct")
    assert not plugin._tasks
    assert not plugin._active_archives


@pytest.mark.asyncio
@pytest.mark.parametrize("is_group", [False, True])
async def test_onebot_file_serialization(
    plugin, event, http, tmp_path, monkeypatch, is_group
):
    monkeypatch.setattr(components, "astrbot_config", {"callback_api_base": ""})
    await plugin.pixiv(event, "123")
    attachment = next(
        component
        for call in event.send.call_args_list
        for component in call.args[0].chain
        if isinstance(component, File)
    )
    bot = MagicMock()
    bot.send_group_msg = AsyncMock()
    bot.send_private_msg = AsyncMock()
    await AiocqhttpMessageEvent.send_message(
        bot, MessageChain([attachment]), is_group=is_group, session_id="123456"
    )
    sender = bot.send_group_msg if is_group else bot.send_private_msg
    sender.assert_awaited_once()
    params = sender.call_args.kwargs
    assert params["group_id" if is_group else "user_id"] == 123456
    archive = next((tmp_path / "saved").glob("*.zip"))
    assert params["message"] == [
        {"type": "file", "data": {"name": archive.name, "file": archive.as_uri()}}
    ]


@pytest.mark.asyncio
async def test_cumulative_size_across_pages(plugin, event, http, tmp_path):
    plugin.config["max_download_mb"] = (len(PNG) + 10) / (1024 * 1024)
    http.routes["/ajax/illust/123/pages"] = web.json_response(
        {
            "body": [
                {"urls": {"original": "https://i.pximg.net/images/123_p0.png"}},
                {"urls": {"original": "https://i.pximg.net/images/123_p1.png"}},
            ]
        }
    )
    http.routes["/images/123_p1.png"] = web.Response(body=PNG, content_type="image/png")
    await plugin.pixiv(event, "123")
    assert "总大小超过" in event.send.call_args.args[0].chain[0].text
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())


@pytest.mark.asyncio
async def test_broken_image_stream(plugin, event, http, tmp_path):
    async def broken(request):
        response = web.StreamResponse(
            headers={"Content-Type": "image/png", "Content-Length": "1000"}
        )
        await response.prepare(request)
        await response.write(PNG)
        request.transport.close()
        return response

    http.routes["/images/123_p0.png"] = broken
    await plugin.pixiv(event, "123")
    assert "网络请求异常" in event.send.call_args.args[0].chain[0].text
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("threshold", "page_count", "direct"),
    [
        (None, 1, True),
        (None, 3, True),
        (None, 4, False),
        (0, 1, False),
        (2, 2, True),
        (2, 3, False),
        (12, 12, True),
    ],
)
async def test_page_threshold_and_image_delivery(
    plugin, event, http, tmp_path, monkeypatch, threshold, page_count, direct
):
    if threshold is None:
        plugin.config.pop("direct_send_max_pages")
    else:
        plugin.config["direct_send_max_pages"] = threshold
    http.routes["/ajax/illust/123/pages"] = web.json_response(
        {
            "body": [
                {"urls": {"original": f"https://i.pximg.net/images/123_p{index}.png"}}
                for index in range(page_count)
            ]
        }
    )
    for index in range(page_count):
        http.routes[f"/images/123_p{index}.png"] = web.Response(
            body=PNG + str(index).encode(), content_type="image/png"
        )
    packing = MagicMock(wraps=plugin_module.shutil.make_archive)
    monkeypatch.setattr(plugin_module.shutil, "make_archive", packing)
    delivered = []

    async def receive(message):
        for component in message.chain:
            if isinstance(component, Image):
                path = Path(component.path)
                assert len(list(path.parent.iterdir())) == page_count
                payload = await AiocqhttpMessageEvent._from_segment_to_dict(component)
                assert payload["type"] == "image"
                assert (
                    payload["data"]["file"]
                    == "base64://" + base64.b64encode(path.read_bytes()).decode()
                )
                delivered.append((path.name, path.read_bytes()))

    event.send.side_effect = receive
    await plugin.pixiv(event, "123")
    archives = list((tmp_path / "saved").glob("*.zip"))
    if direct:
        assert delivered == [
            (f"123_p{index}.png", PNG + str(index).encode())
            for index in range(page_count)
        ]
        assert not archives
        packing.assert_not_called()
        assert "已直接发送" in event.send.call_args.args[0].chain[0].text
    else:
        assert not delivered
        assert len(archives) == 1
        packing.assert_called_once()
        assert "ZIP 已发送" in event.send.call_args.args[0].chain[0].text
    assert not list((tmp_path / "temp").iterdir())
    assert not plugin._tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_page", [1, 2])
@pytest.mark.parametrize(
    "platform", ["aiocqhttp", "qq_official", "qq_official_webhook"]
)
async def test_image_delivery_failure_stops_without_zip(
    plugin, event, http, tmp_path, failed_page, platform
):
    event.get_platform_name.return_value = platform
    plugin.config["direct_send_max_pages"] = 3
    http.routes["/ajax/illust/123/pages"] = web.json_response(
        {
            "body": [
                {"urls": {"original": f"https://i.pximg.net/images/123_p{index}.png"}}
                for index in range(3)
            ]
        }
    )
    for index in range(3):
        http.routes[f"/images/123_p{index}.png"] = web.Response(
            body=PNG, content_type="image/png"
        )
    attempted = []

    async def fail(message):
        for component in message.chain:
            if isinstance(component, Image):
                path = Path(component.path)
                assert path.is_file()
                attempted.append(path.name)
                if len(attempted) == failed_page:
                    raise RuntimeError("image upload failed")

    event.send.side_effect = fail
    await plugin.pixiv(event, "123")
    assert len(attempted) == failed_page
    text = event.send.call_args.args[0].chain[0].text
    assert f"第 {failed_page} 张图片发送失败" in text
    assert f"已发送 {failed_page - 1}/3 张" in text
    assert ("NapCat" in text) is (platform == "aiocqhttp")
    if platform != "aiocqhttp":
        assert "QQ 官方 Bot" in text
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())
    assert not plugin._tasks


@pytest.mark.asyncio
async def test_cancel_during_image_delivery_cleans_temp(plugin, event, http, tmp_path):
    plugin.config["direct_send_max_pages"] = 1
    started = asyncio.Event()
    paths = []

    async def block(message):
        for component in message.chain:
            if isinstance(component, Image):
                paths.append(Path(component.path))
                started.set()
                await asyncio.Event().wait()

    event.send.side_effect = block
    task = asyncio.create_task(plugin.pixiv(event, "123"))
    await asyncio.wait_for(started.wait(), 2)
    assert paths[0].is_file()
    await plugin.terminate()
    assert task.cancelled()
    assert not paths[0].exists()
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())
    assert not plugin._tasks


@pytest.mark.asyncio
async def test_manual_cleanup_preserves_unrelated_files(plugin, event, tmp_path):
    data = tmp_path / "saved"
    generated = data / "123_20261023_120000_abcdef01.zip"
    generated.write_bytes(b"archive")
    unrelated = data / "personal.zip"
    unrelated.write_bytes(b"keep")
    directory = data / "456_20261023_120000_abcdef01.zip"
    directory.mkdir()
    nested = directory / "123_20261023_120000_abcdef02.zip"
    nested.write_bytes(b"keep nested")
    outside = tmp_path / "123_20261023_120000_abcdef03.zip"
    outside.write_bytes(b"keep outside")
    await plugin.pixiv_clean(event)
    assert not generated.exists()
    assert unrelated.read_bytes() == b"keep"
    assert nested.read_bytes() == b"keep nested"
    assert outside.read_bytes() == b"keep outside"
    text = event.send.call_args.args[0].chain[0].text
    assert "删除 1 个 ZIP" in text
    assert "跳过 1 个" in text


@pytest.mark.asyncio
async def test_cleanup_retention_and_manual_override(plugin, event, tmp_path):
    data = tmp_path / "saved"
    old = data / "123_20261021_120000_abcdef01.zip"
    recent = data / "123_20261023_120000_abcdef02.zip"
    old.write_bytes(b"old")
    recent.write_bytes(b"recent")
    past = plugin_module.time.time() - 48 * 3600
    os.utime(old, (past, past))
    assert await plugin._cleanup_archives(24) == (1, 3, 1, 0)
    assert not old.exists() and recent.exists()
    await plugin.pixiv_clean(event)
    assert not recent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("link_type", ["symlink", "reparse"])
async def test_cleanup_never_follows_links(plugin, tmp_path, monkeypatch, link_type):
    path = tmp_path / "saved" / "123_20261023_120000_abcdef01.zip"
    path.write_bytes(b"keep linked target")
    original = Path.lstat
    monkeypatch.setattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, raising=False)

    def linked_info(candidate):
        if candidate == path:
            return SimpleNamespace(
                st_mode=stat.S_IFLNK if link_type == "symlink" else stat.S_IFREG,
                st_file_attributes=0x400 if link_type == "reparse" else 0,
            )
        return original(candidate)

    monkeypatch.setattr(Path, "lstat", linked_info)
    assert await plugin._cleanup_archives() == (0, 0, 1, 0)
    assert path.read_bytes() == b"keep linked target"


@pytest.mark.asyncio
@pytest.mark.parametrize("password", ["", "test-zip-password"])
async def test_cleanup_skips_archive_until_upload_finishes(
    plugin, event, http, tmp_path, password
):
    plugin.config["zip_password"] = password
    protected = []

    async def during_upload(message):
        for component in message.chain:
            if isinstance(component, File):
                path = Path(component.file)
                assert path in plugin._active_archives
                assert await plugin._cleanup_archives() == (0, 0, 1, 0)
                assert path.is_file()
                protected.append(path)

    event.send.side_effect = during_upload
    await plugin.pixiv(event, "123")
    assert len(protected) == 1
    assert not plugin._active_archives
    result = await plugin._cleanup_archives()
    assert result[0] == 1 and result[1] > 0
    assert not protected[0].exists()


@pytest.mark.asyncio
async def test_failed_upload_becomes_eligible_for_cleanup(plugin, event, http):
    event.send.side_effect = [None, RuntimeError("upload failed"), None]
    await plugin.pixiv(event, "123")
    assert not plugin._active_archives
    assert (await plugin._cleanup_archives())[0] == 1


@pytest.mark.asyncio
async def test_parallel_cleanup_is_serialized(plugin, tmp_path):
    for index in range(3):
        (tmp_path / "saved" / f"123_20261023_120000_abcdef0{index}.zip").write_bytes(
            b"zip"
        )
    results = await asyncio.gather(
        plugin._cleanup_archives(), plugin._cleanup_archives()
    )
    assert sum(result[0] for result in results) == 3
    assert sum(result[1] for result in results) == 9
    assert all(result[3] == 0 for result in results)


@pytest.mark.asyncio
async def test_cleanup_reports_file_permission_failure(
    plugin, event, tmp_path, monkeypatch
):
    blocked = tmp_path / "saved" / "123_20261023_120000_abcdef01.zip"
    allowed = tmp_path / "saved" / "123_20261023_120000_abcdef02.zip"
    blocked.write_bytes(b"keep")
    allowed.write_bytes(b"remove")
    original = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("locked")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    await plugin.pixiv_clean(event)
    assert blocked.is_file() and not allowed.exists()
    assert "失败 1 个" in event.send.call_args.args[0].chain[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [PermissionError, RuntimeError])
async def test_cleanup_reports_directory_failure(plugin, event, monkeypatch, error):
    monkeypatch.setattr(
        plugin_module.StarTools, "get_data_dir", MagicMock(side_effect=error)
    )
    await plugin.pixiv_clean(event)
    assert "清理失败" in event.send.call_args.args[0].chain[0].text


@pytest.mark.asyncio
async def test_empty_cleanup(plugin, event):
    await plugin.pixiv_clean(event)
    assert "删除 0 个 ZIP" in event.send.call_args.args[0].chain[0].text


def test_cleanup_command_is_admin_only(event):
    metadata = star_handlers_registry.get_handler_by_full_name(
        f"{plugin_module.__name__}_pixiv_clean"
    )
    permission = next(
        item
        for item in metadata.event_filters
        if isinstance(item, PermissionTypeFilter)
    )
    command = next(
        item for item in metadata.event_filters if isinstance(item, CommandFilter)
    )
    assert command.command_name == "pixiv_clean"
    assert "pixiv清理" in command.alias
    assert permission.permission_type == PermissionType.ADMIN
    event.is_admin.return_value = False
    assert not permission.filter(event, {})
    event.is_admin.return_value = True
    assert permission.filter(event, {})


@pytest.mark.asyncio
async def test_periodic_cleanup_defaults_to_disabled(plugin):
    await plugin.initialize()
    assert plugin._cleanup_task is None


@pytest.mark.asyncio
async def test_periodic_cleanup_converts_hours_to_seconds(plugin, monkeypatch):
    loop = AsyncMock()
    monkeypatch.setattr(plugin, "_cleanup_loop", loop)
    plugin.config.update({"cleanup_interval_hours": 6, "cleanup_retention_hours": 24})
    await plugin.initialize()
    await asyncio.sleep(0)
    loop.assert_awaited_once_with(6 * 3600, 24)
    await plugin.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        {"cleanup_interval_hours": -1},
        {"cleanup_interval_hours": True},
        {"cleanup_interval_hours": 0.5},
        {"cleanup_interval_hours": 1, "cleanup_retention_hours": -1},
        {"cleanup_interval_hours": 1, "cleanup_retention_hours": "24"},
    ],
)
async def test_invalid_periodic_cleanup_config_does_not_start(plugin, config):
    plugin.config.update(config)
    await plugin.initialize()
    assert plugin._cleanup_task is None
    plugin.logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_periodic_cleanup_waits_and_stops_on_unload(plugin, tmp_path):
    path = tmp_path / "saved" / "123_20261023_120000_abcdef01.zip"
    path.write_bytes(b"recent")
    plugin.config.update({"cleanup_interval_hours": 1, "cleanup_retention_hours": 0})
    await plugin.initialize()
    task = plugin._cleanup_task
    await plugin.initialize()
    assert plugin._cleanup_task is task
    await asyncio.sleep(0)
    assert path.is_file() and not task.done()
    await plugin.terminate()
    assert task.cancelled()
    assert path.is_file()


@pytest.mark.asyncio
async def test_periodic_cleanup_applies_retention(plugin, tmp_path, monkeypatch):
    old = tmp_path / "saved" / "123_20261021_120000_abcdef01.zip"
    recent = tmp_path / "saved" / "123_20261023_120000_abcdef02.zip"
    old.write_bytes(b"old")
    recent.write_bytes(b"new")
    past = plugin_module.time.time() - 48 * 3600
    os.utime(old, (past, past))
    original = plugin._cleanup_archives

    async def run(retention):
        result = await original(retention)
        plugin._stopping = True
        return result

    monkeypatch.setattr(plugin, "_cleanup_archives", run)
    await plugin._cleanup_loop(0, 24)
    assert not old.exists() and recent.exists()
    plugin.logger.info.assert_called_once()


@pytest.mark.asyncio
async def test_periodic_cleanup_recovers_after_failure(plugin, monkeypatch):
    calls = []

    async def run(retention):
        calls.append(retention)
        if len(calls) == 1:
            raise OSError("directory unavailable")
        plugin._stopping = True
        return 0, 0, 0, 0

    monkeypatch.setattr(plugin, "_cleanup_archives", run)
    await plugin._cleanup_loop(0, 24)
    assert calls == [24, 24]
    plugin.logger.warning.assert_called_once()
    plugin.logger.info.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("password", ["test-zip-password-233", " 空 格 密码 "])
@pytest.mark.parametrize("page_count", [1, 2])
async def test_zip_is_encrypted_before_delivery(
    plugin, event, http, tmp_path, password, page_count
):
    plugin.config["zip_password"] = password
    http.routes["/ajax/illust/123/pages"] = web.json_response(
        {
            "body": [
                {"urls": {"original": f"https://i.pximg.net/images/123_p{index}.png"}}
                for index in range(page_count)
            ]
        }
    )
    for index in range(page_count):
        http.routes[f"/images/123_p{index}.png"] = web.Response(
            body=PNG + str(index).encode(), content_type="image/png"
        )
    delivered = []

    async def receive(message):
        for component in message.chain:
            if isinstance(component, File):
                path = Path(component.file)
                with pyzipper.AESZipFile(str(path)) as archive:
                    assert archive.namelist() == [
                        f"123_p{index}.png" for index in range(page_count)
                    ]
                    for info in archive.infolist():
                        assert info.flag_bits & 1
                        assert info.wz_aes_strength == 3
                    with pytest.raises(RuntimeError):
                        archive.read("123_p0.png")
                    with pytest.raises(RuntimeError):
                        archive.read("123_p0.png", pwd=b"wrong-password")
                    for index in range(page_count):
                        assert (
                            archive.read(
                                f"123_p{index}.png", pwd=password.encode("utf-8")
                            )
                            == PNG + str(index).encode()
                        )
                delivered.append(path)

    event.send.side_effect = receive
    await plugin.pixiv(event, "123")
    assert len(delivered) == 1
    assert delivered[0].is_file()
    assert "加密 ZIP 已发送" in event.send.call_args.args[0].chain[0].text
    assert password not in str(event.send.call_args_list)
    assert password not in str(plugin.logger.mock_calls)
    assert password not in delivered[0].name
    assert not list((tmp_path / "temp").iterdir())
    assert not plugin._tasks
    assert not plugin._active_archives


@pytest.mark.asyncio
async def test_zip_password_does_not_override_direct_images(
    plugin, event, http, tmp_path, monkeypatch
):
    plugin.config.update(
        {"direct_send_max_pages": 3, "zip_password": "test-zip-password"}
    )
    packing = MagicMock(side_effect=AssertionError("direct images must not be packed"))
    monkeypatch.setattr(plugin, "_pack_archive", packing)
    await plugin.pixiv(event, "123")
    images = [
        component
        for call in event.send.call_args_list
        for component in call.args[0].chain
        if isinstance(component, Image)
    ]
    assert len(images) == 1
    assert "已直接发送" in event.send.call_args.args[0].chain[0].text
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())
    packing.assert_not_called()


@pytest.mark.asyncio
async def test_encryption_failure_never_sends_plain_zip(
    plugin, event, http, tmp_path, monkeypatch
):
    password = "test-password-never-log"
    plugin.config["zip_password"] = password
    monkeypatch.setattr(
        pyzipper.AESZipFile, "write", MagicMock(side_effect=RuntimeError(password))
    )
    plain_pack = MagicMock(side_effect=AssertionError("no unencrypted fallback"))
    monkeypatch.setattr(plugin_module.shutil, "make_archive", plain_pack)
    await plugin.pixiv(event, "123")
    assert "加密 ZIP 生成失败" in event.send.call_args.args[0].chain[0].text
    assert not any(
        isinstance(component, File)
        for call in event.send.call_args_list
        for component in call.args[0].chain
    )
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())
    assert password not in str(event.send.call_args_list)
    assert password not in str(plugin.logger.mock_calls)
    plain_pack.assert_not_called()


@pytest.mark.asyncio
async def test_encrypted_zip_retained_after_upload_failure(
    plugin, event, http, tmp_path
):
    plugin.config["zip_password"] = "test-zip-password"
    event.send.side_effect = [None, RuntimeError("upload failed"), None]
    await plugin.pixiv(event, "123")
    archive_path = next((tmp_path / "saved").glob("*.zip"))
    assert "发送失败" in event.send.call_args.args[0].chain[0].text
    with pyzipper.AESZipFile(str(archive_path)) as archive:
        assert archive.read("123_p0.png", pwd=b"test-zip-password") == PNG
    assert not list((tmp_path / "temp").iterdir())
    assert not plugin._active_archives


@pytest.mark.asyncio
@pytest.mark.parametrize("image_size", ["original", "1200", "540"])
@pytest.mark.parametrize("artwork_proxy", ["", "api.example.com/meta"])
@pytest.mark.parametrize("proxy", ["", "https://images.example.com/pximg"])
@pytest.mark.parametrize("delivery", ["direct", "zip", "encrypted"])
async def test_selected_image_size_for_all_delivery_modes(
    plugin, event, http, tmp_path, image_size, artwork_proxy, proxy, delivery
):
    plugin.config.update(
        {
            "image_size": image_size,
            "artwork_proxy_base_url": artwork_proxy,
            "image_proxy_base_url": proxy,
            "direct_send_max_pages": 3 if delivery == "direct" else 0,
            "zip_password": "test-password" if delivery == "encrypted" else "",
        }
    )
    definitions = {
        "original": ("original", "/img-original/123_p{}.png", PNG),
        "1200": ("regular", "/img-master/123_p{}_master1200.jpg", b"regular-jpeg"),
        "540": (
            "small",
            "/c/540x540_70/img-master/123_p{}_master1200.jpg",
            b"small-jpeg",
        ),
    }
    _, template, content = definitions[image_size]
    suffix = ".png" if image_size == "original" else ".jpg"
    plugin.config["max_download_mb"] = (2 * (len(content) + 1) + 1) / (1024 * 1024)
    pages = [
        {
            "urls": {
                key: "https://i.pximg.net" + path.format(index) + "?token=a%2Fb"
                for key, path, _ in definitions.values()
            }
        }
        for index in range(2)
    ]
    prefix = "/pximg" if proxy else ""
    api_base = (
        "https://api.example.com/meta" if artwork_proxy else "https://www.pixiv.net"
    )
    api_prefix = "/meta" if artwork_proxy else ""
    http.routes[api_prefix + "/ajax/illust/123"] = http.routes.pop("/ajax/illust/123")
    http.routes[api_prefix + "/ajax/illust/123/pages"] = web.json_response(
        {"body": pages}
    )
    http.allowed_hosts = {
        urlsplit(api_base).hostname,
        "images.example.com" if proxy else "i.pximg.net",
    }
    for index in range(2):
        http.routes[prefix + template.format(index)] = web.Response(
            body=content + str(index).encode(),
            content_type="image/png" if image_size == "original" else "image/jpeg",
        )
    delivered = []

    async def receive(message):
        for component in message.chain:
            if isinstance(component, Image):
                path = Path(component.path)
                delivered.append((path.name, path.read_bytes()))

    event.send.side_effect = receive
    await plugin.pixiv(event, "123")
    expected = [
        (f"123_p{index}{suffix}", content + str(index).encode()) for index in range(2)
    ]
    archives = list((tmp_path / "saved").glob("*.zip"))
    if delivery == "direct":
        assert delivered == expected
        assert not archives
    else:
        assert not delivered
        assert len(archives) == 1
        archive_type = pyzipper.AESZipFile if delivery == "encrypted" else ZipFile
        password = b"test-password" if delivery == "encrypted" else None
        with archive_type(str(archives[0])) as archive:
            assert archive.namelist() == [name for name, _ in expected]
            for name, payload in expected:
                assert archive.read(name, pwd=password) == payload
    image_base = proxy or "https://i.pximg.net"
    assert [url for url, _, _ in http.requests] == [
        api_base + "/ajax/illust/123",
        api_base + "/ajax/illust/123/pages",
        image_base + template.format(0) + "?token=a%2Fb",
        image_base + template.format(1) + "?token=a%2Fb",
    ]
    label = "原图" if image_size == "original" else image_size + " 像素预览图"
    assert label in event.send.call_args.args[0].chain[0].text
    assert not list((tmp_path / "temp").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("image_size", ["1200", "540"])
async def test_missing_preview_never_downloads_original(
    plugin, event, http, tmp_path, image_size
):
    plugin.config["image_size"] = image_size
    await plugin.pixiv(event, "123")
    assert (
        image_size + " 像素预览图地址缺失" in event.send.call_args.args[0].chain[0].text
    )
    assert len(http.requests) == 2
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 403, 404])
async def test_preview_download_failure_never_falls_back(
    plugin, event, http, tmp_path, status
):
    plugin.config["image_size"] = "540"
    http.routes["/ajax/illust/123/pages"] = web.json_response(
        {
            "body": [
                {
                    "urls": {
                        "original": "https://i.pximg.net/images/123_p0.png",
                        "small": "https://i.pximg.net/c/540x540_70/123_p0.jpg",
                    }
                }
            ]
        }
    )
    http.routes["/c/540x540_70/123_p0.jpg"] = web.Response(status=status)
    await plugin.pixiv(event, "123")
    assert "下载失败" in event.send.call_args.args[0].chain[0].text
    assert len(http.requests) == 3
    assert http.requests[-1][0] == "https://i.pximg.net/c/540x540_70/123_p0.jpg"
    assert not list((tmp_path / "saved").iterdir())
    assert not list((tmp_path / "temp").iterdir())


@pytest.mark.asyncio
async def test_preview_url_host_is_validated(plugin, event, http, tmp_path):
    plugin.config["image_size"] = "1200"
    http.routes["/ajax/illust/123/pages"] = web.json_response(
        {
            "body": [
                {
                    "urls": {
                        "regular": "https://other.example/123.jpg",
                    }
                }
            ]
        }
    )
    await plugin.pixiv(event, "123")
    assert "不支持的1200 像素预览图地址" in event.send.call_args.args[0].chain[0].text
    assert len(http.requests) == 2
    assert not list((tmp_path / "saved").iterdir())
