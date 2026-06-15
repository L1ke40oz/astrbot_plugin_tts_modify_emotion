# CHANGELOG

## v1.4.2

* 修改：
  * 改进情绪注入机制：从配置文件 `available_emotions` 改为提示词占位符 `{available_emotions}`，与 `qqprofiles` 插件保持一致。
  * 现在所有在 `emotion_params.json` 中定义的情绪均可用，LLM 提示词中通过 `{available_emotions}` 动态注入实际支持的情绪列表。
  * 删除了 `_conf_schema.json` 中的 `available_emotions` 配置项。

* 原因：
  * 简化配置逻辑，避免配置文件和实际能力不一致。
  * 用户只需在 `emotion_params.json` 中添加新情绪，即可自动生效，无需额外配置。

## v1.4.1

* 修复：
  * 修复字面量 `\n`、`\r`、`\t` 未被清洗的问题（LLM 输出 `\n` 字符串会被当作两个字符 `\` 和 `n`）。
  * 现在同时处理真实换行符和字面量转义序列。

* 调整：
  * 增强默认情绪参数幅度，让情绪差异更明显（happy/sad/angry/tender/sexy）。
  * 用户仍可通过 `emotion_params.json` 自由调整。

## v1.4.0

* 新增：
  * 新增 `emotion_params.json`，将 TTS 情绪参数从 `main.py` 中剥离，支持用户自由修改。
  * 支持用户自定义情绪标签，可通过 `available_emotions` 配置限制 LLM 可使用的情绪。
  * 新增 `pass_to_provider` 配置项，可控制是否将 emotion 字段传递给 TTS Provider。

* 修改：
  * 情绪参数加载改为插件初始化时读取配置文件，重载插件后即时生效。
  * 原有硬编码的 `happy/sad/angry/fearful/surprised` Provider 情绪传递逻辑改为配置控制。
  * TTS 标签内容中的 `$`、换行符、回车符会转换为逗号，以便 TTS 产生自然停顿。

* 修复：
  * 修复 TTS 标签内部混入 `$` 或换行符时可能导致语音生成失败的问题。
  * 修复异常情况下可能暴露原始 `<tts>` 标签的问题，失败时优先回退为清洗后的纯文本。

* 原因：
  * 提升情绪参数可配置性、语音标签容错性和插件维护性。
