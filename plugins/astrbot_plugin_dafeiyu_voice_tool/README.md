# 大肥鱼·自主语音（astrbot_plugin_dafeiyu_voice_tool）

给她两个语音能力，让她自己决定什么时候说话、说多久：

- `send_voice(text)`：把一段话用**她自己的声音**念成语音条。一次最长约 **1 分钟**，更长的
  自动切成第二条、第三条……连着发。
- `voice_mode(mode)`：**整段语音聊天**模式。开了以后，她这一轮之后的每条回复都会自动念成
  语音条（不用再调工具），说「打字吧」再关掉。

## 为什么自己做

AstrBot 自带的语音是 `provider_tts_settings.trigger_probability` 决定的：每条回复按概率
转语音，模型既不知道、也控制不了，念出来的还可能是整段解释。本插件把决定权交回给她。

## 依赖

- 一个能用的 TTS 提供商（`provider_tts_settings.provider_id` 指向的那个）：
  - 电脑端：Breeze 本机服务（`breeze-server.exe`，默认 `127.0.0.1:8137`）；
  - 手机端：小米 MiMo（`mimo_tts_api`，`mimo-v2.5-tts`，走 HTTPS，不用显卡）；
  - 也可以换成 `astrbot_plugin_breeze_online_tts` 注册的「大肥鱼语音-Breeze在线」。
- 提供商条目要 `enable: true` 才会被实例化。
- **语音总开关关着也能用**：AstrBot 的 `get_using_tts_provider_async()` 在
  `provider_tts_settings.enable` 为假时一律返回 None（`astrbot/core/provider/manager.py:291`），
  所以本插件拿不到提供商时会自己按 `provider_id` 去实例表里取，取不到再用第一个可用的 TTS。
- 可选 `silk-python`（AstrBot 自带依赖里有）：发出去前把 WAV 转成 QQ 原生 SILK，
  一分钟的语音从 2.3MB 掉到 120KB 左右。

## 时长与分段怎么算

1. 先按 `max_chars`（默认 240 字，中文约 4.5～5 字/秒 ≈ 47～53 秒）按标点切段；
2. 每段合成完，用 WAV 头算出**真实时长**，超过 `max_seconds`（默认 58 秒，QQ 上限 60 秒）
   就在标点处对半切开重新合成（最多递归 3 层）；
3. 每段转 SILK 后单独发一条，两条之间隔 `chunk_gap_ms`（默认 800ms）；
4. 一轮最多发 `max_messages` 条（默认 5），超出的部分不念。

实测（Breeze 本机、音色 dafeiyu）：239 字 → 47.4 秒 → 119KB SILK；130 字 → 26.9 秒 → 67KB。

## 配置项（`_conf_schema.json`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `max_seconds` | 58 | 单条语音最长秒数 |
| `max_chars` | 240 | 每条预备念多少字（第一刀） |
| `max_messages` | 5 | 一轮最多连发几条 |
| `chunk_gap_ms` | 800 | 连发时的间隔 |
| `mode_hours` | 12 | 整段语音模式多久自动关（0 = 不关） |
| `voice_mode_default` | false | 默认就整段语音？ |
| `use_silk` | true | 发之前转 SILK |

## 整段语音模式

- 她说「接下来都用语音跟我说话」「以后发语音」时，模型会调 `voice_mode(mode="on")`；
  状态存在 `data/plugin_data/astrbot_plugin_dafeiyu_voice_tool/voice_mode.json`，
  按会话（`unified_msg_origin`）分别记，`mode_hours` 小时后自动失效。
- 开启后，`on_decorating_result` 会把每条 LLM 文字回复念成语音，然后把文字从消息链里摘掉；
  图片、文件等其它组件照旧保留。
- 想关：说「打字吧」「别发语音了」，她调 `voice_mode(mode="off")`。

## 日志

```
[dafeiyu-voice] 总开关关着，直接取提供商 大肥鱼语音-Breeze
[dafeiyu-voice] 这段合成出来 63.2s 超过 58s，切成两段重来
[dafeiyu-voice] 转 silk：2273324 → 119101 字节
[dafeiyu-voice] 已发语音（239 字, 47.4s, mode）: 我是大肥鱼。今天下午的风有点凉…
[dafeiyu-voice] 整段语音模式：把 369 字念成 2 条语音
[dafeiyu-voice] 丢掉重复文字（相似度 0.87）: ...
```

## 排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 回「（语音合成失败，请直接用文字回复这句话）」 | 合成报错：电脑端 Breeze 没起 / 手机端小米密钥失效或没余额 | 电脑：面板点「启动 Breeze」；手机：检查「大肥鱼语音-MiMo」 |
| 整段语音模式下还是只有文字 | 模式没开（看日志有没有「打开整段语音模式」），或这一轮已经发过语音条 | `voice_mode(mode="status")` 查状态；`voice_mode.json` 里看该 umo 的 `on` |
| 语音太长被 QQ 拒收 | `max_seconds` 设太大 | 保持 ≤58；`use_silk: true` 发的是标准 silk，60 秒内都收 |
| 模型说「未找到指定的工具: send_voice」 | 人设的**工具白名单**被写成了空列表（`tools: []` 表示一个工具都不给；`null` 才是不限制） | 把该人设的 `tools`/`skills` 改回 `null` |
| 语音条收不到、日志显示发送失败 | 平台不接受 silk | 把 `use_silk` 设 false（退回发 WAV） |