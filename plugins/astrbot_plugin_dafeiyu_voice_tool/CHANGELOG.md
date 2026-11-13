# 更新日志

## v0.2.0 — 2026-11-04
- 新增 `voice_mode` 工具：说「接下来都用语音跟我说话」就开启**整段语音聊天**模式，
  之后每条回复都会自动念成语音条（按 `unified_msg_origin` 分别记状态，`mode_hours` 小时后自动关）。
- `send_voice` 从「60 字封顶」放开到**约一分钟**：按 `max_chars` 切段、合成后按真实时长
  二次校验（超 `max_seconds` 就对半再切），超长自动变成第二条、第三条……连着发。
- 发出去前把 WAV 转成 QQ 原生 SILK（`silk-python`），一分钟从 2.3MB 降到 ~120KB；
  转换失败自动退回原文件，`use_silk: false` 可关。
- 新增 `_conf_schema.json`：`max_seconds` / `max_chars` / `max_messages` / `chunk_gap_ms` /
  `mode_hours` / `voice_mode_default` / `use_silk`。
- 文本清洗更狠：Markdown、链接、括号里的动作描写都会在念之前去掉。

## v0.1.1 — 2026-11-03
- 修：手机端 `send_voice` 一直回「语音现在用不了」。
  根因是 AstrBot 的规矩 —— `provider_tts_settings.enable` 关掉就一律不给 TTS 提供商（`astrbot/core/provider/manager.py:291`），
  而「整条回复都转语音」那个插件偏偏要求总开关关掉，两边打架。现在拿不到提供商时，按配置里点名的
  `provider_id` 直接去实例表里取，取不到再拿第一个可用的 TTS。
- 于是电脑端走 Breeze 本机、手机端走小米 MiMo，都能发语音了。

## v0.1.0 — 2026-11-02
- 新增 `send_voice` 工具：她自己挑一句话用语音发，不再靠概率掷骰子。
- 发过语音后，这一轮重复的文字回复自动丢弃（按相似度判断，正经补充的内容保留）。
- 超过 60 字自动在标点处断开，不截半句。
