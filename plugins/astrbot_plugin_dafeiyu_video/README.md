# 大肥鱼·看视频（astrbot_plugin_dafeiyu_video）

有人发视频/语音时，她能「看」到画面、「听」到声音，而不是只收到一句 `[视频]`。

## 做什么

- **看视频**：用 ffmpeg 均匀截几张画面交给对话模型看（默认最多 6 张，只看前 180 秒）。
- **听声音**：三条路，自动挨个试 —— ①本机语音识别（`asr_dir`，装了 sherpa-onnx + SenseVoice，免费）；
  ②**小米 MiMo**（`mimo-v2.5-asr`，key 借 AstrBot 里「小米」那家服务商，不用另填）；
  ③硅基流动 key（`asr_key`）。视频音轨和 QQ 语音都走同一套。
- 私聊发的视频都看；群里要 **@ 她** 才看（`group_need_at`），不然太费钱。
- 语音私聊、群里都听（`listen_voice`），群里的语音没法 @ 她，转文字后由群聊插件决定接不接。

## 依赖

- **ffmpeg**：`ffmpeg_path` 留空会自动在 PATH、winget 安装位置里找；找不到她会说自己看不了视频。
- 可选：本机 ASR 文件夹；或小米那家服务商（AstrBot「服务提供商」里有 `xiaomi` 就能用 MiMo）；或硅基流动 key。

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `ffmpeg_path` | 空 | 留空自动找；手机上是 `/usr/bin/ffmpeg` |
| `max_seconds` | `180` | 最多看前多少秒 |
| `max_frames` | `6` | 最多截几张（一张图约 0.002 元，短视频会少截） |
| `group_need_at` | `true` | 群里要 @ 才看 |
| `keep_frames` | `false` | 截的画面是否留在聊天记录里；打开后能追问画面细节，但之后每轮都多带这几张图 |
| `asr_dir` | 空 | 本机语音识别文件夹，如 `E:\dafeiyu\asr`；手机上留空 |
| `asr_key` | 空 | 硅基流动 key（兜底用） |
| `asr_model` | `FunAudioLLM/SenseVoiceSmall` | 硅基流动的转写模型 |
| `mimo_asr` | `true` | 用小米 MiMo 听声音（key 借 xiaomi 服务商） |
| `mimo_asr_model` | `mimo-v2.5-asr` | 小米识别模型（别填成合成的 `mimo-v2.5-tts`） |
| `mimo_first` | `false` | 打开就让小米排在本机识别前面（更准、有网更稳；本机识别免费） |
| `listen_voice` | `true` | 听 QQ 语音 |

## 文件

- `main.py`：事件钩子、帧预算、ASR 调度（`_hear` / `_look`，优先级顺序在这里）
- `video.py`：ffmpeg 截帧、音轨抽取、ASR 调用（`transcribe_local` / `transcribe_mimo` / `transcribe`）

## 排错

| 现象 | 处理 |
| --- | --- |
| 她说「看不了视频」 | 装 ffmpeg 或填 `ffmpeg_path` |
| 只看到画面没听到声音 | 三种 ASR 都没成：看日志里 `[dafeiyu_video] 语音识别（xxx）没成` 的那一行；小米要确认 AstrBot 里有 `xiaomi` 服务商且 key 有效 |
| 小米报 `HTTP 401/403` | key 不对或没余额；AstrBot → 服务提供商 → 小米 里换 key |
| 手机上一直本机识别 | 那是很久以前推过去的 Windows 路径，重跑一次面板「同步到手机」会改成 `/usr/bin/ffmpeg` + 空 `asr_dir` |
| 群里发视频没反应 | 没 @ 她；或 `group_need_at` 关着但群聊插件没决定回复 |
| 追问画面她说没看清 | `keep_frames` 默认关闭，画面只在这一轮用 |