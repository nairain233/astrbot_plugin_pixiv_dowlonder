import asyncio
import base64
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit
from zipfile import ZipFile

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from astrbot.api.event import MessageChain, MessageEventResult
from astrbot.api.message_components import File, Image
from astrbot.core.message import components
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
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
    )

    async def handle(request):
        """Serve a configured response or asynchronous streaming handler.

        Args:
            request: Local test HTTP request.

        Returns:
            Configured response or a not-found response.
        """
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
        local_url = f"http://127.0.0.1:{port}{parsed.path}"
        if parsed.query:
            local_url += f"?{parsed.query}"
        return await original_request(session, method, local_url, **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "_request", route)
    try:
        yield state
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proxy", ["", "https://images.example.com", "https://images.example.com/pximg/"]
)
async def test_complete_archive_and_cookie_isolation(
    plugin, event, http, tmp_path, proxy
):
    plugin.config.update(
        {"pixiv_cookie": "PHPSESSID=test-secret", "image_proxy_base_url": proxy}
    )
    prefix = "/pximg" if proxy.endswith("/pximg/") else ""
    http.routes["/ajax/illust/123/pages"] = web.json_response(
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
    http.routes[f"{prefix}/images/123_p0.png"] = web.Response(
        body=PNG, content_type="image/png", headers={"Set-Cookie": "unexpected=secret"}
    )
    http.routes[f"{prefix}/images/123_p1.jpg"] = web.Response(
        body=b"second-original", content_type="image/jpeg"
    )
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
    for url, options, jar in http.requests:
        assert options["headers"]["Referer"] == "https://www.pixiv.net/"
        assert isinstance(jar, aiohttp.DummyCookieJar)
        if url.startswith("https://www.pixiv.net/"):
            assert options["headers"]["Cookie"] == "PHPSESSID=test-secret"
            assert options["allow_redirects"] is False
        else:
            assert "Cookie" not in options["headers"]
    expected = proxy.rstrip("/") if proxy else "https://i.pximg.net"
    assert http.requests[2][0] == expected + "/images/123_p0.png?token=a%2Fb"
    assert "test-secret" not in str(plugin.logger.mock_calls)


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
        ({"image_proxy_base_url": "ftp://example.com"}, "图片代理地址"),
        ({"image_proxy_base_url": "https://user:password@example.com"}, "图片代理地址"),
        ({"image_proxy_base_url": "https://example.com?token=secret"}, "图片代理地址"),
        ({"image_proxy_base_url": "https://example.com/#fragment"}, "图片代理地址"),
        ({"pixiv_cookie": "foo=bar\nSecret: value"}, "Cookie 不能包含换行"),
        ({"direct_send_max_pages": -1}, "直接发送页数上限"),
        ({"direct_send_max_pages": 1.5}, "直接发送页数上限"),
        ({"direct_send_max_pages": "3"}, "直接发送页数上限"),
        ({"direct_send_max_pages": True}, "直接发送页数上限"),
    ],
)
async def test_invalid_config(plugin, event, http, config, message):
    plugin.config.update(config)
    await plugin.pixiv(event, "123")
    assert message in event.send.call_args.args[0].chain[0].text
    assert not http.requests
    assert not plugin._tasks


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
async def test_upload_failure_preserves_archive(plugin, event, http, tmp_path):
    event.send.side_effect = [None, RuntimeError("upload failed"), None]
    await plugin.pixiv(event, "123")
    archive = next((tmp_path / "saved").glob("*.zip"))
    assert str(archive) in event.send.call_args.args[0].chain[0].text
    assert "发送失败" in event.send.call_args.args[0].chain[0].text
    assert not list((tmp_path / "temp").iterdir())
    assert ZipFile(archive).testzip() is None


@pytest.mark.asyncio
async def test_timeout(plugin, event, http, monkeypatch, tmp_path):
    monkeypatch.setattr(
        plugin, "_download", AsyncMock(side_effect=asyncio.TimeoutError)
    )
    await plugin.pixiv(event, "123")
    assert "请求超时" in event.send.call_args.args[0].chain[0].text
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
async def test_cancel_during_zip_waits_for_worker_cleanup(
    plugin, event, http, monkeypatch, tmp_path
):
    started = threading.Event()
    release = threading.Event()
    original = plugin_module.shutil.make_archive

    def pack(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(plugin_module.shutil, "make_archive", pack)
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
async def test_image_delivery_failure_stops_without_zip(
    plugin, event, http, tmp_path, failed_page
):
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
