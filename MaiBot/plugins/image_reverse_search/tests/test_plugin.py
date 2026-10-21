"""插件 SDK 集成测试。

当前仓库的轻量测试环境不一定安装 maibot-plugin-sdk，因此整组测试在该依赖
缺失时跳过；核心调度测试不依赖 SDK。
"""

from types import SimpleNamespace
from typing import List, Tuple

import base64
import logging

import pytest

pytest.importorskip("maibot_sdk")

from maibot_sdk.context import PluginContext  # noqa: E402

from plugins.image_reverse_search.core import (  # noqa: E402
    MODE_ANIME,
    EngineSearchOutcome,
    EngineStatus,
    ImageInput,
    SearchReport,
    SearchResult,
    format_report,
)
from plugins.image_reverse_search.plugin import (  # noqa: E402
    ImageReverseSearchPlugin,
    create_plugin,
)


class FakeSend:
    """记录发送内容的假发送能力。"""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    async def text(self, text: str, stream_id: str) -> bool:
        self.calls.append((text, stream_id))
        return True


class FakeBackend:
    """按引擎返回预设结果。"""

    def __init__(self, outcomes: dict[str, object]) -> None:
        self._outcomes = outcomes
        self.calls: List[str] = []

    async def search(self, engine: str, image: ImageInput, timeout: float) -> object:
        del image
        del timeout
        self.calls.append(engine)
        outcome = self._outcomes[engine]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, EngineSearchOutcome):
            return outcome
        return list(outcome)


class FakeMessageCapability:
    """仅模拟 Host RPC，消息返回值经过真实 SDK 代理及解包逻辑。"""

    def __init__(self, messages: dict[str, dict[str, object]]) -> None:
        self._messages = messages
        self.calls: List[Tuple[str, str, bool]] = []
        self._sdk = PluginContext("maibot-team.image-reverse-search", rpc_call=self._call_host).message

    async def _call_host(self, method: str, plugin_id: str, payload: dict[str, object]) -> dict[str, object]:
        """保持宿主原始响应格式，避免测试跳过 SDK 边界。"""
        assert method == "cap.call"
        assert plugin_id == "maibot-team.image-reverse-search"
        assert payload["capability"] == "message.get_by_id"
        args = payload["args"]
        message_id = args["message_id"]
        self.calls.append((message_id, args["chat_id"], args["include_binary_data"]))
        return {"success": True, "message": self._messages.get(message_id)}

    async def get_by_id(
        self, message_id: str, chat_id: str = "", include_binary_data: bool = False
    ) -> dict[str, object] | None:
        return await self._sdk.get_by_id(message_id, chat_id=chat_id, include_binary_data=include_binary_data)


def build_plugin(
    messages: dict[str, dict[str, object]] | None = None,
) -> tuple[ImageReverseSearchPlugin, FakeSend]:
    """构造已注入假上下文和默认配置的插件。"""

    plugin = ImageReverseSearchPlugin()
    plugin.set_plugin_config(plugin.build_default_config())
    send = FakeSend()
    message_capability = FakeMessageCapability(messages or {})
    plugin._ctx = SimpleNamespace(  # type: ignore[assignment]
        logger=logging.getLogger("test.image_reverse_search"),
        send=send,
        message=message_capability,
    )
    plugin._rebuild_service()
    return plugin, send


def group_message(
    text: str,
    image_base64: str = "",
    *,
    message_id: str = "m2",
    reply_to: str = "",
) -> dict[str, object]:
    """构造群聊事件消息。"""

    segments: list[dict[str, object]] = [{"type": "text", "data": text}]
    if reply_to:
        segments.append({"type": "reply", "data": {"target_message_id": reply_to}})
    if image_base64:
        segments.append({"type": "image", "data": "", "binary_data_base64": image_base64})
    return {
        "message_id": message_id,
        "session_id": "stream-1",
        "processed_plain_text": text,
        "raw_message": segments,
        "reply_to": reply_to or None,
        "message_info": {"group_info": {"group_id": "123"}},
    }


@pytest.mark.asyncio
async def test_plugin_components_are_registered() -> None:
    plugin = create_plugin()
    components = plugin.get_components()
    names = {(component["name"], component["type"]) for component in components}

    assert ("image_reverse_search_message", "EVENT_HANDLER") in names
    assert ("image_reverse_search", "TOOL") in names
    assert ("image_search_help", "COMMAND") in names
    assert ("image_reverse_search_command", "COMMAND") in names
    command = next(component for component in components if component["name"] == "image_reverse_search_command")
    assert command["metadata"]["include_binary_data"] is True
    tool = next(component for component in components if component["name"] == "image_reverse_search")
    assert "当前消息或引用消息" in tool["metadata"]["description"]
    for parameter in tool["metadata"]["parameters"]:
        assert parameter["required"] is False


@pytest.mark.asyncio
async def test_group_message_hit_replies_and_blocks() -> None:
    plugin, send = build_plugin()
    plugin._service._backend = FakeBackend(  # type: ignore[union-attr]
        {"saucenao": [SearchResult("saucenao", "作品", "画师", "https://example.com/a", 96.0, 96.0)]}
    )
    encoded = base64.b64encode(b"fake-image").decode("ascii")

    result = await plugin.handle_message(group_message("/搜图", encoded))

    assert result == {"blocked": True}
    assert len(send.calls) == 1
    reply, stream_id = send.calls[0]
    assert stream_id == "stream-1"
    assert "命中引擎：SauceNAO" in reply
    assert "标题：作品" in reply


@pytest.mark.asyncio
async def test_command_without_image_replies_usage() -> None:
    plugin, send = build_plugin()

    result = await plugin.handle_message(group_message("/搜图"))

    assert result == {"blocked": True}
    assert "附带" in send.calls[0][0]


@pytest.mark.asyncio
async def test_private_message_is_ignored_when_group_only() -> None:
    plugin, send = build_plugin()
    message = group_message("/搜图", base64.b64encode(b"fake-image").decode("ascii"))
    message["message_info"] = {"group_info": None}

    result = await plugin.handle_message(message)

    assert result is None
    assert send.calls == []


@pytest.mark.asyncio
async def test_tool_supports_anime_mode_and_returns_report() -> None:
    plugin, send = build_plugin()
    plugin._service._backend = FakeBackend(  # type: ignore[union-attr]
        {"tracemoe": [SearchResult("tracemoe", "番剧 第1集", "未知", "https://anilist.co/anime/1", 91.0, 91.0)]}
    )
    encoded = base64.b64encode(b"fake-image").decode("ascii")

    result = await plugin.handle_search_tool(image_base64=encoded, mode=MODE_ANIME, stream_id="stream-1")

    assert result["success"] is True
    assert "trace.moe" in result["content"]
    assert result["data"]["hit"]["engine"] == "tracemoe"
    assert send.calls == []


@pytest.mark.asyncio
async def test_tool_rejects_both_url_and_base64() -> None:
    plugin, _send = build_plugin()

    result = await plugin.handle_search_tool(image_url="https://example.com/a.jpg", image_base64="YWJjZA==")

    assert result["success"] is False


@pytest.mark.asyncio
async def test_engine_failure_still_formats_fallback_report() -> None:
    plugin, send = build_plugin()
    plugin._service._backend = FakeBackend(  # type: ignore[union-attr]
        {
            "saucenao": RuntimeError("should be hidden"),
            "ascii2d": [SearchResult("ascii2d", "作品", "画师", "https://example.com/a", None, 85.0)],
        }
    )
    encoded = base64.b64encode(b"fake-image").decode("ascii")

    await plugin.handle_message(group_message("/搜图", encoded))

    reply = send.calls[0][0]
    assert "命中引擎：ascii2d" in reply
    assert "should be hidden" not in reply


@pytest.mark.asyncio
async def test_config_update_rebuilds_engine_order() -> None:
    plugin, _send = build_plugin()
    config = plugin.build_default_config()
    config["search"]["illustration_engines"] = ["iqdb", "saucenao"]

    await plugin.on_config_update("self", config, "1.0.1")

    assert plugin._settings is not None
    assert plugin._settings.illustration_engines == ("iqdb", "saucenao")


def test_default_orders_match_acceptance() -> None:
    config = ImageReverseSearchPlugin.build_default_config()
    assert config["search"]["illustration_engines"] == ["saucenao", "ascii2d", "iqdb"]
    assert config["search"]["anime_engines"] == ["tracemoe", "saucenao", "ascii2d", "iqdb"]
    assert config["search"]["engine_timeout"] == 15.0
    assert config["saucenao"]["api_key"] == ""


def test_format_report_for_low_scores_contains_attempts() -> None:
    config = ImageReverseSearchPlugin.build_default_config()
    assert config["search"]["hit_threshold"] == 80.0
    report = SearchReport(
        mode="illustration",
        threshold=80.0,
        attempts=[],
        hit=None,
        low_results=[],
    )
    assert "所有引擎均未命中" in format_report(report)


def test_engine_status_values_are_stable() -> None:
    assert EngineStatus.HIT == "hit"
    assert EngineStatus.SKIPPED == "skipped"


@pytest.mark.asyncio
async def test_command_reads_quoted_message_original_image() -> None:
    encoded = base64.b64encode(b"quoted-image").decode("ascii")
    stored = group_message("原图", encoded, message_id="m1")
    plugin, send = build_plugin({"m1": stored})
    plugin._service._backend = FakeBackend(  # type: ignore[union-attr]
        {
            "saucenao": EngineSearchOutcome(skipped=True, reason="未配置 SauceNAO API key"),
            "ascii2d": [SearchResult("ascii2d", "引用图", "画师", "https://example.com/q", None, 85.0)],
        }
    )

    result = await plugin.handle_search_command(
        message=group_message("/搜图", reply_to="m1"),
        stream_id="stream-1",
    )

    assert result[0] is True
    capability = plugin._ctx.message
    assert capability.calls == [("m1", "stream-1", True)]
    assert plugin._service._backend.calls == ["saucenao", "ascii2d"]  # type: ignore[union-attr]
    assert "命中引擎：ascii2d" in send.calls[0][0]


@pytest.mark.asyncio
async def test_tool_reads_anchor_message_image_without_url_or_base64() -> None:
    encoded = base64.b64encode(b"anchor-image").decode("ascii")
    plugin, _send = build_plugin({"m1": group_message("图", encoded, message_id="m1")})
    plugin._service._backend = FakeBackend(  # type: ignore[union-attr]
        {"saucenao": [SearchResult("saucenao", "当前图", "画师", "https://example.com/current", 97.0, 97.0)]}
    )

    result = await plugin.handle_search_tool(anchor_message_id="m1", stream_id="stream-1")

    assert result["success"] is True
    assert "当前图" in result["content"]
    assert plugin._service._backend.calls == ["saucenao"]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_tool_reads_quoted_image_when_anchor_has_none() -> None:
    encoded = base64.b64encode(b"quoted-only-image").decode("ascii")
    plugin, _send = build_plugin(
        {
            "m1": group_message("/搜图", message_id="m1"),
            "m0": group_message("原图", encoded, message_id="m0"),
        }
    )
    plugin._service._backend = FakeBackend(  # type: ignore[union-attr]
        {"saucenao": [SearchResult("saucenao", "引用图", "画师", "https://example.com/q", 95.0, 95.0)]}
    )

    result = await plugin.handle_search_tool(
        anchor_message_id="m1",
        reply_to_message_id="m0",
        stream_id="stream-1",
    )

    assert result["success"] is True
    assert plugin._ctx.message.calls == [("m1", "stream-1", True), ("m0", "stream-1", True)]
    assert "引用图" in result["content"]


@pytest.mark.asyncio
async def test_missing_stored_image_does_not_run_search() -> None:
    """SDK 查无消息返回 None，取图失败必须与引擎无结果区分。"""
    plugin, _send = build_plugin()
    backend = FakeBackend({})
    plugin._service._backend = backend
    result = await plugin.handle_search_tool(anchor_message_id="missing", stream_id="stream-1")
    assert result["success"] is False
    assert "未向反查引擎发送请求" in result["content"]
    assert plugin._ctx.message.calls == [("missing", "stream-1", True)]
    assert backend.calls == []


@pytest.mark.asyncio
async def test_command_handler_uses_same_service() -> None:
    plugin, send = build_plugin()
    plugin._service._backend = FakeBackend(  # type: ignore[union-attr]
        {"saucenao": [SearchResult("saucenao", "命令图", "画师", "https://example.com/c", 93.0, 93.0)]}
    )
    encoded = base64.b64encode(b"command-image").decode("ascii")

    result = await plugin.handle_search_command(
        message=group_message("/搜图", encoded),
        stream_id="stream-1",
    )

    assert result[0] is True
    assert "命令图" in send.calls[0][0]
