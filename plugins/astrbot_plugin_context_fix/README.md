# 大肥鱼 · 上下文清洗

发请求前给 `req.contexts` 洗一遍澡：**图片看完就不重复上传**、丢掉 think 块、修掉坏图片地址、
**把历史里的听歌笔记压成占位**，历史太长时还会**自动压缩**。省 token，也避免严格接口 400。

## 为什么会有这个插件

两件事撞在一起：

1. **图会反复进上下文**。AstrBot 把 history 原样带上，一张 86KB 的 jpeg base64 就有一两万 token，
   聊几轮图片就把上下文顶爆 —— 而且那些图模型早就看过了，重复上传纯属浪费。
2. **老图地址是坏的**。2026-11-04 晚上出现过「AI 说看不见我发的图」：
   `Chat Model xiaomi/mimo-v2.6-flash request error: Error code: 400 - Param Incorrect`，
   当晚出现 38 次。挖出来是历史里两类毒块：
   - `{"type":"image_url","image_url":{"url":"omitted"}}` —— 落库时原文被抹成 7 个字符（`attachments` 表也是空的，无法还原）；
   - `{"type":"think","think":"…"}` —— assistant 的推理块，MiMo 不认，400 变成 `Invalid request parameters`。

   而 `sanitize_contexts_by_modalities()` 只在 provider **不支持**图片时才替换图片块，
   xiaomi 声明支持，于是一个 7 字符的假地址被原样发出去 → 每次都 400，全部落到 qwen 兜底。

## 它做什么

| 块 | 处理 |
| --- | --- |
| **user 消息里的图片** | 换成一行文字占位（`placeholder`）—— 当轮已经看过，历史里不必再传 |
| `tool`/`assistant` 消息里的图片 | 不动（那是模型刚拿到的结果） |
| `{"type":"think",…}` | 丢掉 |
| 历史里的听歌笔记（含歌词、风格分析） | 换成一行占位；当前这一轮刚听的歌不受影响 |
| 超过 `compress_trigger_chars` 的旧 user/assistant 文本 | 保留开头若干字 + `〔较早内容已压缩〕` |
| 超过 `compress_tool_chars` 的旧工具结果 | 换成一行「较早的工具结果已省略」 |
| 其它消息里 url 不是 `data:`/`http://`/`https://` 的图片 | 换成占位（`fix_image`） |
| `text` / `audio_url` / `input_audio` | 原样保留 |
| 不认识的块 | 有 `text` 就降级成文本，否则丢掉 |
| 清完变空的消息 | `tool` 角色和带 `tool_calls` 的 assistant 保留，其它丢掉 |

**当前这一轮的图不受影响**：新图走 `req.image_urls`，不在 `contexts` 里，
而钩子（`on_llm_request`）在 `prepare_request_images()` 之前触发，所以这一轮模型照样看得见。

## 实测

同一段真实历史（`data_v4.db` 的 `conversations`，161 条消息、260477 字符）直发 `mimo-v2.6-flash`：

| 版本 | 结果 |
| --- | --- |
| 什么都不做 | 400 `the provided url is not valid` |
| 只丢 think | 400 `Invalid request parameters` |
| 丢 think + 修坏图（v0.1.0） | 200，`prompt_tokens 17360`，`image_tokens 1762` |
| 丢 think + 历史图片全部转文字（v0.2.0） | 200，`prompt_tokens 15660`，`image_tokens 0` |

图越多、图越大，省得越狠（一张 86KB 的图单独就有一两千 image token，还要占上传体积）。

## 配置

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `strip_images` | bool | `true` | 历史图片转文字占位（本插件主要功能） |
| `fix_image` | bool | `true` | 修正其它消息里的坏图片地址 |
| `drop_think` | bool | `true` | 丢 think 块 |
| `strip_music` | bool | `true` | 历史听歌笔记压成占位 |
| `compress_enable` | bool | `true` | 历史过长时自动压缩 |
| `compress_trigger_chars` | int | `60000` | 超过多少字符触发压缩（约 3.5 万 token） |
| `compress_keep_recent` | int | `30` | 最近多少条消息保持原样（约 15 轮） |
| `compress_old_chars` | int | `160` | 旧的 user/assistant 消息保留多少字 |
| `compress_tool_chars` | int | `220` | 旧的工具结果超过多少字换成省略占位 |
| `placeholder` | string | `[之前发过的图片，你看过了，内容已省略]` | 图片换成的文字 |
| `log_every` | int | `60` | 日志最小间隔（秒） |

## 日志

```
[ctx-fix] 历史瘦身：图片 6 张转文字、think 80 个、坏图 0 个 → 剩 161 条消息（累计省下 6 张图 / 80 个推理块）
```

## 排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 模型说「看不清你刚才那张图」 | 图不是这一轮发的，历史上只剩占位 | 正常行为；把 `strip_images` 关掉就会一直带着原图（费 token） |
| 仍然 400 `Param Incorrect` | 还有别的块不被接受 | 看 `run.log` 原始错误体的 `param`，再补规则 |
| 日志一条都没有 | 这段会话本来就没图、没 think | 正常 |
| 想回看老图 | 老图原文当年就没存 | 救不回来，重新发一张 |

## 听歌笔记与自动压缩

听歌插件以前把「你听到的是：…歌词…」作为普通内容塞进用户消息，AstrBot 落库后每一轮都会重发。
现在两处一起改：

1. 听歌插件注入的笔记改成 `TextPart(...).mark_as_temp()`，**从源头不落库**；
2. 本插件仍会扫描历史，把旧版本已经落库的听歌笔记压成一行占位（当前这一轮不受影响）。

自动压缩只动 `req.contexts`：保留最近 `compress_keep_recent` 条原样，更早的文本压短、旧工具结果省略。
**不删消息、不改 role/tool_calls**，不会把工具调用和结果拆散。
AstrBot 会把这批清理后的上下文当成新一轮历史保存；但 LivingMemory 有自己的会话库和总结游标，
记忆召回又走 `extra_user_content_parts`（临时片段），所以这里压掉的只是 AstrBot 侧的原始长文本，
不会覆盖、删除或干扰记忆模块里的会话记录和记忆条目，旧细节仍可由它按需召回。

- 只改 `req.contexts`，不碰数据库、`provider_settings`、模型选择。
- 对 Anthropic 一类需要原样回放 thinking 的接口，可以把 `drop_think` 关掉。