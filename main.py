import re
import copy
import json
import traceback
from pathlib import Path

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.event.filter import on_llm_request, on_decorating_result
from astrbot.api.message_components import Plain, Record
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core import logger

# TTS 标签正则：匹配 <tts>...</tts> 或 <tts:emotion>...</tts> 或 <tts:emotion>...</tts:emotion>
TTS_START_PATTERN = re.compile(r"<tts(?::(\w+))?>")
TTS_END_PATTERN = re.compile(r"</tts(?::\w+)?>")  # 匹配 </tts> 或 </tts:emotion>
TTS_START_TAG = "<tts"  # 用于快速检测
TTS_END_TAG = "</tts"  # 用于快速检测（改为前缀匹配）
BOUNDARY_SEPARATORS = "$"
BOUNDARY_SEPARATOR_PATTERN = re.compile(rf"[{re.escape(BOUNDARY_SEPARATORS)}]+$")
LEADING_BOUNDARY_SEPARATOR_PATTERN = re.compile(
    rf"^[{re.escape(BOUNDARY_SEPARATORS)}]+"
)

# ─── 默认情绪参数配置（用户可在 emotion_params.json 中覆盖）───
EMOTION_CONFIG_FILE = "emotion_params.json"
DEFAULT_EMOTION_CONFIG = {
    "baseline": {"speed": 1.0, "pitch": 0, "vol": 1.0},
    "strength": 0.7,
    "emotions": {
        "happy": {"speed": 0.08, "pitch": 0.3, "vol": 0.1, "pass_to_provider": True, "description": "开心"},
        "sad": {"speed": -0.1, "pitch": -0.4, "vol": -0.1, "pass_to_provider": True, "description": "难过"},
        "angry": {"speed": 0.12, "pitch": 0.2, "vol": 0.15, "pass_to_provider": True, "description": "生气"},
        "fearful": {"speed": 0.1, "pitch": 0.5, "vol": 0.05, "pass_to_provider": True, "description": "害怕"},
        "surprised": {"speed": 0.05, "pitch": 0.4, "vol": 0.15, "pass_to_provider": True, "description": "惊讶"},
        "tender": {"speed": -0.05, "pitch": -0.2, "vol": -0.05, "pass_to_provider": False, "description": "温柔"},
        "sexy": {"speed": -0.15, "pitch": -0.5, "vol": 0.08, "pass_to_provider": False, "description": "性感"},
        "neutral": {"speed": 0, "pitch": 0, "vol": 0, "pass_to_provider": False, "description": "中性"},
    },
}
VOICE_PARAM_RANGES = {
    "speed": (0.5, 2.0),
    "pitch": (-12.0, 12.0),
    "vol": (0.1, 2.0),
}
CLEANUP_PUNCTUATION_PATTERN = re.compile(r"[，,]+")
WHITESPACE_PATTERN = re.compile(r"\s+")


class TTSModifyPlugin(Star):
    """对 LLM 回复中 <tts>/<tts:emotion> 标签包裹的文本进行 TTS 转换。
    
    支持情绪标签：<tts:happy>、<tts:sad>、<tts:angry>、<tts:tender> 等。
    LLM 根据自身当前情绪选择标签，插件自动微调语音参数。
    不带情绪标签的 <tts> 使用默认参数。
    """

    CONFIG_KEY_TTS_SETTINGS = "provider_tts_settings"
    CONFIG_KEY_ENABLE = "enable"
    CONFIG_KEY_TTS_PROMPT = "tts_prompt"
    CONFIG_KEY_NOTIFY_FAILURE = "notify_on_failure"

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context, config)
        self.config = config or {}
        self.emotion_config = None

    async def initialize(self):
        """插件初始化：从配置生成 emotion_params.json，然后加载。"""
        self._sync_config_to_emotion_params()
        self._load_emotion_config()

    def _sync_config_to_emotion_params(self):
        """从 self.config 读取配置并写入 emotion_params.json。"""
        try:
            # 读取配置界面中的参数
            baseline_speed = self.config.get("baseline_speed", 1.0)
            baseline_pitch = self.config.get("baseline_pitch", 0.0)
            baseline_vol = self.config.get("baseline_vol", 1.0)
            emotion_strength = self.config.get("emotion_strength", 0.7)
            emotion_params_list = self.config.get("emotion_params", [])

            # 构建 emotion_params.json 格式
            emotion_config = {
                "baseline": {
                    "speed": baseline_speed,
                    "pitch": baseline_pitch,
                    "vol": baseline_vol,
                },
                "strength": emotion_strength,
                "emotions": {}
            }

            # 转换 emotion_params 列表为字典格式
            for emotion_item in emotion_params_list:
                emotion_name = emotion_item.get("emotion_name", "").strip()
                if not emotion_name:
                    continue
                
                emotion_config["emotions"][emotion_name] = {
                    "speed": emotion_item.get("speed_offset", 0.0),
                    "pitch": emotion_item.get("pitch_offset", 0.0),
                    "vol": emotion_item.get("vol_offset", 0.0),
                    "pass_to_provider": emotion_item.get("pass_to_provider", False),
                    "description": emotion_item.get("display_name", emotion_name),
                }

            # 写入 emotion_params.json
            config_path = Path(__file__).parent / EMOTION_CONFIG_FILE
            config_path.write_text(
                json.dumps(emotion_config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"已从配置界面同步参数到 {config_path}")

        except Exception as e:
            logger.exception(f"同步配置到 emotion_params.json 失败: {e}")

    def _load_emotion_config(self):
        """加载情绪参数配置文件，失败时使用默认配置。"""
        config_path = Path(__file__).parent / EMOTION_CONFIG_FILE
        try:
            if config_path.exists():
                content = config_path.read_text(encoding="utf-8")
                loaded = json.loads(content)
                self.emotion_config = self._validate_emotion_config(loaded)
                logger.info(f"已加载情绪参数配置: {config_path}")
            else:
                # 配置文件不存在，创建默认配置
                self.emotion_config = copy.deepcopy(DEFAULT_EMOTION_CONFIG)
                config_path.write_text(
                    json.dumps(DEFAULT_EMOTION_CONFIG, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info(f"已创建默认情绪参数配置: {config_path}")
        except Exception as e:
            logger.exception(f"加载情绪参数配置失败，使用内置默认值: {e}")
            self.emotion_config = copy.deepcopy(DEFAULT_EMOTION_CONFIG)

        logger.info(f"可用情绪列表: {self._format_available_emotions()}")

    @staticmethod
    def _validate_emotion_config(config: dict) -> dict:
        """校验并修正情绪配置参数，确保在安全范围内。"""
        validated = copy.deepcopy(DEFAULT_EMOTION_CONFIG)
        
        # 校验 baseline
        if "baseline" in config and isinstance(config["baseline"], dict):
            for key in ["speed", "pitch", "vol"]:
                if key in config["baseline"]:
                    validated["baseline"][key] = config["baseline"][key]
        
        # 校验 strength
        if "strength" in config:
            validated["strength"] = max(0.0, min(1.0, float(config["strength"])))
        
        # 校验 emotions
        if "emotions" in config and isinstance(config["emotions"], dict):
            for emotion, params in config["emotions"].items():
                if not isinstance(params, dict):
                    continue
                validated["emotions"][emotion] = {
                    "speed": params.get("speed", 0),
                    "pitch": params.get("pitch", 0),
                    "vol": params.get("vol", 0),
                    "pass_to_provider": params.get("pass_to_provider", False),
                    "description": params.get("description", emotion),
                }
        
        return validated

    def _format_available_emotions(self) -> str:
        """从 emotion_params.json 加载结果中动态生成可用情绪列表。"""
        try:
            emotions = self.emotion_config.get("emotions", {}) if self.emotion_config else {}
            if not emotions:
                return "（暂无）"
            return "、".join(emotions.keys())
        except Exception as e:
            logger.exception(f"格式化可用情绪列表失败: {e}")
            return "（暂无）"

    def _calculate_emotion_params(self, emotion: str) -> dict | None:
        """根据情绪标签计算微调后的 voice_setting 参数。"""
        if not self.emotion_config:
            return None
        
        emotions = self.emotion_config["emotions"]
        if emotion not in emotions:
            logger.warning(f"未知情绪标签: {emotion}，将使用默认参数")
            return None
        
        if emotion == "neutral":
            return None
        
        baseline = self.emotion_config["baseline"]
        strength = self.emotion_config["strength"]
        adj = emotions[emotion]
        
        # 计算最终参数
        speed = baseline["speed"] + adj["speed"] * strength
        pitch = baseline["pitch"] + adj["pitch"] * strength
        vol = baseline["vol"] + adj["vol"] * strength
        
        # 参数范围限制
        speed = max(VOICE_PARAM_RANGES["speed"][0], min(VOICE_PARAM_RANGES["speed"][1], speed))
        pitch = max(VOICE_PARAM_RANGES["pitch"][0], min(VOICE_PARAM_RANGES["pitch"][1], pitch))
        vol = max(VOICE_PARAM_RANGES["vol"][0], min(VOICE_PARAM_RANGES["vol"][1], vol))
        
        return {
            "speed": round(speed, 2),
            "pitch": round(pitch, 1),
            "vol": round(vol, 2),
            "pass_to_provider": adj.get("pass_to_provider", False),
        }

    @staticmethod
    def _cleanup_tts_content(text: str) -> str:
        """清洗 TTS 标签内文本，移除不适合 TTS 的特殊字符。
        
        清洗规则：
        - $ 分隔符 -> 逗号（产生停顿）
        - 换行符/制表符（真实或字面量） -> 逗号或空格
        - 连续逗号 -> 单个逗号
        - 多余空白 -> 单个空格
        """
        # 1. 替换分隔符和换行为逗号
        text = text.replace("$", "，")
        
        # 处理真实换行符（chr(10)=LF, chr(13)=CR, chr(9)=Tab）
        text = text.replace(chr(10), "，").replace(chr(13), "，").replace(chr(9), " ")
        
        # 处理字面量转义序列（LLM 可能输出的两字符字符串）
        text = text.replace("\\n", "，").replace("\\r", "，").replace("\\t", " ")

        # 2. 合并连续逗号
        text = CLEANUP_PUNCTUATION_PATTERN.sub("，", text)

        # 3. 合并多余空白
        text = WHITESPACE_PATTERN.sub(" ", text)

        # 4. 清除首尾空白
        text = text.strip()

        return text

    # ─── 辅助方法 ───

    def _own_plugin_name(self) -> str:
        """Best-effort resolve this plugin's registered name for session checks."""
        try:
            from astrbot.core.star.star import star_map

            meta = star_map.get(self.__class__.__module__)
            if meta and meta.name:
                return meta.name
        except Exception:
            pass
        return "astrbot_plugin_tts_modify"

    async def _session_inactive(self, umo: str) -> bool:
        """Whether this plugin is disabled for the session via AstrBot custom rules.

        AstrBot's per-session plugin management only filters command / message
        handlers, not lifecycle hooks (on_llm_request / on_decorating_result).
        So we query the session config ourselves and skip when disabled. Fails
        open (returns False) when the API is unavailable.
        """
        try:
            from astrbot.core.star.session_plugin_manager import (
                SessionPluginManager,
            )
        except Exception:
            return False
        try:
            enabled = await SessionPluginManager.is_plugin_enabled_for_session(
                umo, self._own_plugin_name()
            )
            return not enabled
        except Exception:
            return False

    async def _session_tts_disabled(self, umo: str) -> bool:
        """Whether TTS has been disabled for this session via AstrBot custom rules.

        The framework's native TTS path is gated on
        ``SessionServiceManager.should_process_tts_request``, but this plugin
        builds Record components directly in on_decorating_result, bypassing
        that gate. We honour the same per-session TTS toggle here so disabling
        TTS for a session also stops <tts> tags from being voiced (tags are
        stripped to plain text instead). Fails open (returns False) when the
        API is unavailable.
        """
        try:
            from astrbot.core.star.session_llm_manager import (
                SessionServiceManager,
            )
        except Exception:
            return False
        try:
            return not await SessionServiceManager.is_tts_enabled_for_session(umo)
        except Exception:
            return False

    def _get_global_config(self, event: AstrMessageEvent):
        """安全地获取全局/会话配置。"""
        try:
            return self.context.get_config(event.unified_msg_origin)
        except (KeyError, Exception):
            pass
        try:
            return self.context.get_config()
        except Exception as e:
            logger.error(f"TTS插件获取配置失败: {e}")
            return None

    @staticmethod
    def _trim_boundary_separators(text: str, *, leading: bool = False) -> str:
        if leading:
            return LEADING_BOUNDARY_SEPARATOR_PATTERN.sub("", text)
        return BOUNDARY_SEPARATOR_PATTERN.sub("", text)

    @classmethod
    def _append_text_segment(cls, segments: list[dict], text: str) -> None:
        stripped = text.strip()
        if not stripped:
            return
        if segments and segments[-1]["type"] == "tts":
            stripped = cls._trim_boundary_separators(stripped, leading=True).strip()
        stripped = cls._trim_boundary_separators(stripped).strip()
        if stripped:
            segments.append({"type": "text", "content": stripped})

    @classmethod
    def _split_by_tts_tags(cls, text: str) -> list[dict]:
        """
        将文本按 <tts>...</tts> 或 <tts:emotion>...</tts> 或 <tts:emotion>...</tts:emotion> 标签拆分。

        返回:
          {"type": "text", "content": "普通文本"}
          {"type": "tts",  "content": "TTS文本", "emotion": "happy"|None}
        """
        segments = []
        cursor = 0
        text_length = len(text)

        while cursor < text_length:
            start_match = TTS_START_PATTERN.search(text, cursor)
            end_match = TTS_END_PATTERN.search(text, cursor)

            if start_match is None and end_match is None:
                cls._append_text_segment(segments, text[cursor:])
                break

            # 处理孤立的 </tts> 或 </tts:emotion>
            if end_match is not None and (start_match is None or end_match.start() < start_match.start()):
                cls._append_text_segment(segments, text[cursor:end_match.start()])
                cursor = end_match.end()
                continue

            # 开始标签前的文本
            if start_match.start() > cursor:
                cls._append_text_segment(segments, text[cursor:start_match.start()])

            emotion = start_match.group(1)  # None if <tts>, str if <tts:emotion>
            content_start = start_match.end()

            end_match = TTS_END_PATTERN.search(text, content_start)
            if end_match is None:
                cls._append_text_segment(segments, text[content_start:])
                break

            tts_content = text[content_start:end_match.start()].strip()
            tts_content = cls._trim_boundary_separators(
                cls._trim_boundary_separators(tts_content, leading=True),
            ).strip()
            # 清洗 TTS 内容（移除 $、换行等特殊字符）
            tts_content = cls._cleanup_tts_content(tts_content) if hasattr(cls, '_cleanup_tts_content') else tts_content
            if tts_content:
                segments.append({
                    "type": "tts",
                    "content": tts_content,
                    "emotion": emotion,
                })
            cursor = end_match.end()

        if not segments:
            cleaned = TTS_START_PATTERN.sub("", text)
            cleaned = TTS_END_PATTERN.sub("", cleaned).strip()
            if cleaned:
                segments.append({"type": "text", "content": cleaned})

        return segments

    @staticmethod
    def _validate_audio_path(audio_path: str) -> bool:
        """校验音频文件路径是否在 AstrBot data 目录下（安全检查）。"""
        try:
            audio_file = Path(audio_path).resolve()
            expected_dir = Path(get_astrbot_data_path()).resolve()
            return audio_file.is_relative_to(expected_dir)
        except Exception:
            return False

    # ─── Hook: LLM 请求前注入 TTS 提示词 ───

    @on_llm_request()
    async def on_llm_req(self, event: AstrMessageEvent, request: ProviderRequest):
        if await self._session_inactive(event.unified_msg_origin):
            return

        global_config = self._get_global_config(event)
        if not global_config:
            return

        # 检查全局 TTS 是否启用
        provider_tts_settings = global_config.get(self.CONFIG_KEY_TTS_SETTINGS, {})
        if not provider_tts_settings.get(self.CONFIG_KEY_ENABLE, False):
            return

        # 尊重会话级 TTS 开关（自定义规则里关闭 TTS 时不注入提示词）
        if await self._session_tts_disabled(event.unified_msg_origin):
            return

        # 检查 TTS Provider 是否可用
        tts_provider = self.context.get_using_tts_provider(event.unified_msg_origin)
        if not tts_provider:
            return

        # 注入提示词，替换 {available_emotions} 占位符
        tts_prompt = self.config.get(self.CONFIG_KEY_TTS_PROMPT, "")
        if tts_prompt:
            available_emotions = self._format_available_emotions()
            request.system_prompt += "\n" + tts_prompt.format(available_emotions=available_emotions)

    # ─── Hook: 结果装饰——处理 TTS 标签 ───

    @on_decorating_result(priority=13)
    async def on_decorate(self, event: AstrMessageEvent):
        if await self._session_inactive(event.unified_msg_origin):
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        # 获取配置
        global_config = self._get_global_config(event)
        if not global_config:
            return

        provider_tts_settings = global_config.get(self.CONFIG_KEY_TTS_SETTINGS, {})

        # 会话级 TTS 开关：若被自定义规则关闭，则不生成语音，
        # 仅将 <tts> 标签剥离为纯文本（避免标签泄露）。
        session_tts_enabled = not await self._session_tts_disabled(
            event.unified_msg_origin
        )

        # 快速检测：是否有任何 Plain 组件包含 TTS 标签或残缺标签
        has_tts_tag = any(
            isinstance(comp, Plain)
            and (TTS_START_TAG in comp.text or TTS_END_TAG in comp.text)
            for comp in result.chain
        )
        if not has_tts_tag:
            return

        # 获取 TTS 服务提供商
        tts_provider = self.context.get_using_tts_provider(event.unified_msg_origin)
        if not tts_provider:
            logger.warning(
                f"会话 {event.unified_msg_origin} 检测到 <tts> 标签，"
                f"但未找到 TTS 服务提供商，将剥离标签并显示文本。"
            )

        # 构建新的消息链
        new_chain = []
        modified = False

        for comp in result.chain:
            if isinstance(comp, Plain) and (
                TTS_START_TAG in comp.text or TTS_END_TAG in comp.text
            ):
                components = await self._process_tts_text(
                    comp.text,
                    tts_provider,
                    provider_tts_settings,
                    session_tts_enabled,
                )
                new_chain.extend(components)
                modified = True
            else:
                new_chain.append(comp)

        if modified:
            result.chain = new_chain

    async def _process_tts_text(
        self,
        text: str,
        tts_provider,
        provider_settings: dict,
        session_tts_enabled: bool = True,
    ) -> list:
        """
        处理包含 <tts> 标签的文本，将其拆分为 Plain 和 Record 组件。

        关键逻辑：
          - 标签外的文本 → Plain 组件
          - 标签内的文本 → 调用 TTS 生成 → Record 组件
          - 自动处理标签与普通文本之间没有分隔符的情况
        """
        segments = self._split_by_tts_tags(text)
        components = []

        tts_enabled = (
            provider_settings.get(self.CONFIG_KEY_ENABLE, False)
            and session_tts_enabled
        )
        dual_output = provider_settings.get("dual_output", False)
        use_file_service = provider_settings.get("use_file_service", False)
        notify_failure = self.config.get(self.CONFIG_KEY_NOTIFY_FAILURE, False)

        for seg in segments:
            if seg["type"] == "text":
                # 普通文本，直接作为 Plain 组件
                components.append(Plain(seg["content"]))

            elif seg["type"] == "tts":
                tts_content = seg["content"]
                emotion = seg.get("emotion")
                audio_component = None

                if tts_enabled and tts_provider:
                    audio_component = await self._generate_tts_audio(
                        tts_content, tts_provider, use_file_service, emotion
                    )

                if audio_component:
                    # TTS 生成成功
                    components.append(audio_component)
                    if dual_output:
                        components.append(Plain(tts_content))
                else:
                    # TTS 不可用或生成失败，回退为纯文本
                    if not tts_enabled:
                        logger.warning(
                            "检测到 <tts> 标签，但 TTS 未启用（全局关闭或本会话已禁用），剥离标签显示文本。"
                        )
                    if notify_failure and tts_enabled and tts_provider:
                        components.append(Plain(f"[TTS失败] {tts_content}"))
                    else:
                        components.append(Plain(tts_content))

        return components

    async def _generate_tts_audio(
        self, tts_content: str, tts_provider, use_file_service: bool,
        emotion: str | None = None,
    ) -> Record | None:
        """调用 TTS 生成音频。如果有情绪标签则临时微调语音参数。"""
        original_voice_setting = None
        try:
            # 有情绪标签且 provider 支持 voice_setting 时微调参数
            if emotion and hasattr(tts_provider, "voice_setting"):
                emotion_params = self._calculate_emotion_params(emotion)
                if emotion_params:
                    original_voice_setting = copy.deepcopy(tts_provider.voice_setting)
                    tts_provider.voice_setting["speed"] = emotion_params["speed"]
                    tts_provider.voice_setting["pitch"] = emotion_params["pitch"]
                    tts_provider.voice_setting["vol"] = emotion_params["vol"]
                    
                    # 根据配置决定是否向 Provider 传递 emotion 字段
                    if emotion_params.get("pass_to_provider", False):
                        tts_provider.voice_setting["emotion"] = emotion
                    
                    logger.info(
                        f"TTS 情绪微调: '{emotion}' → "
                        f"speed={emotion_params['speed']}, "
                        f"pitch={emotion_params['pitch']}, "
                        f"vol={emotion_params['vol']}, "
                        f"pass_to_provider={emotion_params.get('pass_to_provider', False)}"
                    )

            audio_path = await tts_provider.get_audio(tts_content)
            if not audio_path:
                logger.error(f"TTS 返回空路径，内容: {tts_content[:50]}...")
                return None

            # 安全校验
            if not self._validate_audio_path(audio_path):
                logger.error(f"TTS 返回路径不安全: {audio_path}")
                return None

            # 构建 Record 组件
            record = Record.fromFileSystem(audio_path, text=tts_content)

            # 如果需要文件服务，注册并获取 URL
            if use_file_service:
                try:
                    url = await record.register_to_file_service()
                    record.url = url
                    record.file = url
                except Exception as e:
                    logger.warning(f"文件服务注册失败，使用本地路径: {e}")

            return record

        except Exception as e:
            logger.error(f"TTS 生成失败: {e}")
            logger.debug(traceback.format_exc())
            return None
        finally:
            if original_voice_setting is not None and hasattr(tts_provider, "voice_setting"):
                tts_provider.voice_setting = original_voice_setting
