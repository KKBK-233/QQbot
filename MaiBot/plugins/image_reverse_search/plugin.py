"""群内图片反查插件入口。

基于 PicImageSearch 封装 SauceNAO、ascii2d、IQDB 与 trace.moe，按配置顺序
回退调用。插件本身不直接访问主程序模块，所有宿主能力通过 ``self.ctx`` 获取。
"""

from logging import Logger, getLogger
from typing import Any, Literal, Optional

from maibot_sdk import (
    Command,
    EventHandler,
    Field,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import EventType, ToolParameterInfo, ToolParamType

from .core import (
    DEFAULT_ANIME_ENGINES,
    DEFAULT_ILLUSTRATION_ENGINES,
    MODE_ANIME,
    MODE_ILLUSTRATION,
    BackendOptions,
    ImageInput,
    ImageSearchService,
    PicImageSearchBackend,
    SearchSettings,
    SecretRedactingFilter,
    extract_image,
    extract_message_text,
    extract_reply_target_id,
    format_report,
    format_usage,
    is_group_message,
    parse_search_command,
    resolve_stream_id,
)

_ENGINE_NAME = Literal["saucenao", "ascii2d", "iqdb", "tracemoe"]
_MODE_NAME = Literal["illustration", "anime"]
_LOG_FILTER_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用群内图片反查")
    config_version: str = Field(default="1.0.1", description="配置版本")
    group_only: bool = Field(default=True, description="是否只响应群聊消息")


class SearchConfig(PluginConfigBase):
    """搜索与回退配置。"""

    __ui_label__ = "搜索"
    __ui_order__ = 1

    default_mode: _MODE_NAME = Field(default=MODE_ILLUSTRATION, description="未显式指定时的默认查询模式")
    hit_threshold: float = Field(default=80.0, description="命中阈值（0-100），达到后立即停止")
    engine_timeout: float = Field(default=15.0, description="单个引擎的默认超时时间（秒）")
    max_low_results: int = Field(default=5, description="全部不中时最多展示的低分候选数量")
    ascii2d_implicit_score: float = Field(
        default=85.0,
        description="ascii2d 不返回相似度时用于命中判断的隐式分数（展示仍标为未知）",
    )
    max_image_mb: float = Field(default=10.0, description="允许搜索的最大图片大小（MB）")
    illustration_engines: list[_ENGINE_NAME] = Field(
        default_factory=lambda: list(DEFAULT_ILLUSTRATION_ENGINES),
        description="二次元插画引擎顺序",
    )
    anime_engines: list[_ENGINE_NAME] = Field(
        default_factory=lambda: list(DEFAULT_ANIME_ENGINES),
        description="番剧截图引擎顺序",
    )
    engine_timeouts: dict[str, float] = Field(
        default_factory=dict,
        description='按引擎覆盖超时时间，例如 {"saucenao": 8}',
    )


class SauceNaoConfig(PluginConfigBase):
    """SauceNAO 配置。"""

    __ui_label__ = "SauceNAO"
    __ui_order__ = 2

    api_key: str = Field(default="", description="SauceNAO API key；留空时读取环境变量")
    api_key_env: str = Field(default="SAUCENAO_API_KEY", description="SauceNAO API key 的环境变量名")
    numres: int = Field(default=10, description="请求的结果数量")
    hide: int = Field(default=0, description="SauceNAO hide 参数")
    min_similarity: float = Field(default=0.0, description="SauceNAO minsim 参数，0 表示保留低分结果")


class TraceMoeConfig(PluginConfigBase):
    """trace.moe 配置。"""

    __ui_label__ = "trace.moe"
    __ui_order__ = 3

    api_key: str = Field(default="", description="trace.moe API key；留空时读取环境变量")
    api_key_env: str = Field(default="TRACE_MOE_API_KEY", description="trace.moe API key 的环境变量名")
    mute: bool = Field(default=False, description="预览视频是否静音")


class NetworkConfig(PluginConfigBase):
    """网络配置。"""

    __ui_label__ = "网络"
    __ui_order__ = 4

    proxies: str = Field(default="", description="可选的 HTTP(S) 代理地址")
    verify_ssl: bool = Field(default=True, description="是否校验 HTTPS 证书")


class ImageReverseSearchConfig(PluginConfigBase):
    """图片反查插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    saucenao: SauceNaoConfig = Field(default_factory=SauceNaoConfig)
    tracemoe: TraceMoeConfig = Field(default_factory=TraceMoeConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)


class ImageReverseSearchPlugin(MaiBotPlugin):
    """群内图片反查插件。"""

    config_model = ImageReverseSearchConfig

    def __init__(self) -> None:
        """初始化插件状态。"""

        super().__init__()
        self._service: Optional[ImageSearchService] = None
        self._settings: Optional[SearchSettings] = None
        self._log_filter: Optional[SecretRedactingFilter] = None
        self._log_filter_loggers: list[Logger] = []

    async def on_load(self) -> None:
        """插件加载完成后构造搜索服务。"""

        self._rebuild_service()

    async def on_unload(self) -> None:
        """插件卸载时释放搜索服务引用并移除日志过滤器。"""

        self._remove_log_filter()
        self._service = None
        self._settings = None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """配置热更新后重建搜索服务。"""

        del version
        if scope != "self":
            return
        if isinstance(config_data, dict) and "search" in config_data:
            self.set_plugin_config(config_data)
        self._rebuild_service()

    @Command(
        "image_search_help",
        description="查看图片反查用法",
        pattern=r"^/(?:搜图|搜番)帮助$",
    )
    async def handle_help(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """发送图片反查用法。"""

        del kwargs
        await self._send_text(format_usage(), stream_id)
        return True, "已发送图片反查用法", 2

    @Command(
        "image_reverse_search_command",
        description="对当前消息或引用消息中的图片做反查",
        pattern=r"^/(?:搜图|搜番)(?:\s+.*)?$",
        include_binary_data=True,
    )
    async def handle_search_command(
        self,
        message: Any = None,
        text: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理命令链中的 /搜图 与 /搜番。"""

        del kwargs
        if not self.config.plugin.enabled or not isinstance(message, dict):
            return False, "图片反查插件未启用", False
        if self.config.plugin.group_only and not is_group_message(message):
            return False, "图片反查仅支持群聊", False

        mode = parse_search_command(extract_message_text(message) or text)
        if mode is None:
            return False, "未识别到搜图命令", False

        image, error = await self._resolve_event_image(message, stream_id or resolve_stream_id(message))
        if image is None:
            await self._send_text(error or self._missing_image_text(), stream_id or resolve_stream_id(message))
            return True, "未找到可反查的图片", True

        reply = await self._search_and_format(image, mode)
        await self._send_text(reply, stream_id or resolve_stream_id(message))
        return True, "已发送图片反查结果", True

    @Tool(
        "image_reverse_search",
        description=(
            "对群聊当前消息或引用消息里的原图做反向图片搜索，优先自动读取宿主提供的当前消息/引用消息图片，"
            "不需要用户发 URL 或 Base64。适合群友发图问画师、出处、相似图，或发番剧截图问是哪部番。"
            "默认按 SauceNAO、ascii2d、IQDB 顺序回退，anime 模式优先 trace.moe。"
        ),
        parameters=[
            ToolParameterInfo(
                name="image_url",
                param_type=ToolParamType.STRING,
                description="兼容参数：仅当调用上下文没有可读取的群消息图片时才填 HTTP(S) 图片地址",
                required=False,
            ),
            ToolParameterInfo(
                name="image_base64",
                param_type=ToolParamType.STRING,
                description="兼容参数：仅当调用上下文没有可读取的群消息图片时才填图片 Base64",
                required=False,
            ),
            ToolParameterInfo(
                name="mode",
                param_type=ToolParamType.STRING,
                description="illustration 表示二次元插画，anime 表示番剧截图；不确定时留空",
                required=False,
            ),
        ],
    )
    async def handle_search_tool(
        self,
        image_url: str = "",
        image_base64: str = "",
        mode: str = "",
        stream_id: str = "",
        anchor_message_id: str = "",
        reply_to_message_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """供 LLM 调用的图片反查工具；自动使用当前/引用消息原图。"""

        del kwargs
        service = self._service
        settings = self._settings
        if service is None or settings is None or not self.config.plugin.enabled:
            return {"success": False, "content": "图片反查插件未启用"}

        try:
            image = self._build_tool_image(image_url, image_base64, settings.max_image_bytes)
        except ValueError as exc:
            return {"success": False, "content": f"图片参数无效：{exc}"}
        if image is None:
            image = await self._resolve_tool_image(
                anchor_message_id=anchor_message_id,
                reply_to_message_id=reply_to_message_id,
                stream_id=stream_id,
            )
        if image is None:
            return {
                "success": False,
                "content": "当前消息和引用消息的原图未能读取，本次未向反查引擎发送请求。",
            }

        try:
            resolved_mode = self._resolve_mode(mode)
            report = await service.search(image, mode=resolved_mode)
            text = format_report(report)
        except ValueError as exc:
            return {"success": False, "content": f"图片反查参数无效：{exc}"}
        except Exception as exc:
            self.ctx.logger.error("图片反查工具异常: image=%s error_type=%s", image.fingerprint(), type(exc).__name__)
            return {"success": False, "content": "图片反查服务暂时不可用"}

        # 工具结果交回模型组织最终回复，避免工具直发后模型再发一遍造成重复。
        return {"success": True, "content": text, "data": report.to_dict()}

    @EventHandler(
        "image_reverse_search_message",
        description="识别群内 /搜图 与 /搜番 命令并执行图片反查",
        event_type=EventType.ON_MESSAGE_PRE_PROCESS,
        intercept_message=True,
        weight=50,
    )
    async def handle_message(self, message: Any = None, **kwargs: Any) -> Optional[dict[str, Any]]:
        """处理带图片的群内搜图命令。"""

        del kwargs
        service = self._service
        settings = self._settings
        if service is None or settings is None or not self.config.plugin.enabled:
            return None
        if not isinstance(message, dict):
            return None
        if self.config.plugin.group_only and not is_group_message(message):
            return None

        mode = parse_search_command(extract_message_text(message))
        if mode is None:
            return None

        stream_id = resolve_stream_id(message)
        image, error = await self._resolve_event_image(message, stream_id)
        if image is None:
            await self._send_text(error or self._missing_image_text(), stream_id)
            return {"blocked": True}

        reply = await self._search_and_format(image, mode)
        await self._send_text(reply, stream_id)
        return {"blocked": True}

    def _rebuild_service(self) -> None:
        """按当前配置重建后端与服务。"""

        config = self.config
        search = config.search
        settings = SearchSettings(
            illustration_engines=tuple(search.illustration_engines),
            anime_engines=tuple(search.anime_engines),
            hit_threshold=search.hit_threshold,
            engine_timeout=search.engine_timeout,
            engine_timeouts=dict(search.engine_timeouts),
            max_low_results=search.max_low_results,
            ascii2d_implicit_score=search.ascii2d_implicit_score,
            max_image_bytes=int(search.max_image_mb * 1024 * 1024),
        )
        backend = PicImageSearchBackend(
            BackendOptions(
                sauce_api_key=config.saucenao.api_key,
                sauce_api_key_env=config.saucenao.api_key_env,
                trace_api_key=config.tracemoe.api_key,
                trace_api_key_env=config.tracemoe.api_key_env,
                sauce_numres=config.saucenao.numres,
                sauce_hide=config.saucenao.hide,
                sauce_min_similarity=config.saucenao.min_similarity,
                ascii2d_implicit_score=settings.ascii2d_implicit_score,
                trace_mute=config.tracemoe.mute,
                proxies=config.network.proxies,
                verify_ssl=config.network.verify_ssl,
            )
        )
        self._install_log_filter(backend.options.secrets())
        self._settings = settings
        self._service = ImageSearchService(
            backend,
            settings=settings,
            logger=self.ctx.logger,
            secrets=backend.options.secrets(),
        )

    async def _resolve_event_image(self, message: dict[str, Any], stream_id: str) -> tuple[Optional[ImageInput], str]:
        """从当前消息或引用消息中取得原图，返回图片和错误提示。"""

        settings = self._settings
        if settings is None:
            return None, "图片反查服务尚未初始化"
        current_error = ""
        try:
            image = extract_image(message, settings.max_image_bytes)
        except ValueError as exc:
            image = None
            current_error = str(exc)
        if image is not None:
            return image, ""

        target_id = extract_reply_target_id(message)
        if target_id:
            quoted_image = await self._load_image_by_id(target_id, stream_id)
            if quoted_image is not None:
                return quoted_image, ""

        if current_error:
            return None, f"图片读取失败：{current_error}"
        return None, self._missing_image_text()

    async def _resolve_tool_image(
        self,
        *,
        anchor_message_id: str,
        reply_to_message_id: str,
        stream_id: str,
    ) -> Optional[ImageInput]:
        """按宿主注入的消息 ID 读取当前消息或引用消息原图。"""

        for message_id in (anchor_message_id, reply_to_message_id):
            normalized = str(message_id or "").strip()
            if not normalized:
                continue
            image = await self._load_image_by_id(normalized, stream_id)
            if image is not None:
                return image
        return None

    async def _load_image_by_id(self, message_id: str, stream_id: str) -> Optional[ImageInput]:
        """通过消息能力读取指定消息原始图片，优先使用本地缓存/下载后的字节。"""

        settings = self._settings
        if settings is None or not message_id:
            return None
        try:
            target_message = await self.ctx.message.get_by_id(
                message_id=message_id,
                chat_id=stream_id,
                include_binary_data=True,
            )
        except Exception as exc:
            self.ctx.logger.warning("读取引用消息图片失败: error_type=%s", type(exc).__name__)
            return None
        # SDK 已将 Host 的 {success, message} 解包为消息字典；查无消息时返回 None。
        if not isinstance(target_message, dict):
            return None
        try:
            return extract_image(target_message, settings.max_image_bytes)
        except ValueError as exc:
            self.ctx.logger.warning("引用消息图片无效: error_type=%s", type(exc).__name__)
            return None

    async def _search_and_format(self, image: ImageInput, mode: str) -> str:
        """统一执行搜索并格式化；引擎异常已在 service 内降级。"""

        service = self._service
        if service is None:
            return "图片反查服务尚未初始化。"
        try:
            report = await service.search(image, mode=mode)
            return format_report(report)
        except Exception as exc:
            self.ctx.logger.error(
                "图片反查消息处理异常: image=%s error_type=%s", image.fingerprint(), type(exc).__name__
            )
            return "图片反查失败：搜索服务暂时不可用。"

    @staticmethod
    def _missing_image_text() -> str:
        """提示用户如何提供原图。"""

        return "请在发送图片的同一条消息中附带 `/搜图` 或 `/搜番`，或引用一张图片后再发送命令。"

    def _install_log_filter(self, secrets: list[str]) -> None:
        """给第三方 HTTP 日志安装密钥脱敏过滤器。"""

        self._remove_log_filter()
        log_filter = SecretRedactingFilter(secrets)
        for logger_name in _LOG_FILTER_LOGGERS:
            logger = getLogger(logger_name)
            logger.addFilter(log_filter)
            self._log_filter_loggers.append(logger)
        self._log_filter = log_filter

    def _remove_log_filter(self) -> None:
        """移除本插件安装的日志过滤器。"""

        if self._log_filter is not None:
            for logger in self._log_filter_loggers:
                logger.removeFilter(self._log_filter)
        self._log_filter_loggers = []
        self._log_filter = None

    def _resolve_mode(self, mode: str) -> str:
        """把工具参数规范化为查询模式。"""

        normalized = str(mode or "").strip().lower()
        if not normalized:
            return self.config.search.default_mode
        if normalized in {MODE_ILLUSTRATION, "illust", "插画", "二次元"}:
            return MODE_ILLUSTRATION
        if normalized in {MODE_ANIME, "番剧", "动画"}:
            return MODE_ANIME
        raise ValueError("mode 只能是 illustration 或 anime")

    @staticmethod
    def _build_tool_image(image_url: str, image_base64: str, max_bytes: int) -> Optional[ImageInput]:
        """校验并构造工具入参图片。"""

        url = str(image_url or "").strip()
        encoded = str(image_base64 or "").strip()
        if url and encoded:
            raise ValueError("image_url 与 image_base64 只能提供一个")
        if url:
            return ImageInput.from_url(url)
        if encoded:
            return ImageInput.from_base64(encoded, max_bytes)
        return None

    async def _send_text(self, text: str, stream_id: str) -> None:
        """发送文本；发送失败只记录异常类型，不记录外部内容。"""

        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:
            self.ctx.logger.error("图片反查结果发送失败: error_type=%s", type(exc).__name__)


def create_plugin() -> ImageReverseSearchPlugin:
    """创建图片反查插件实例。"""

    return ImageReverseSearchPlugin()
