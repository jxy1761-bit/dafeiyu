# 自研插件

这些插件运行在 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 4.29.x 上。除特别说明外，均按仓库根目录 AGPL-3.0 许可提供。

> 插件中的主人 ID / 服务器地址均为占位值（`10000000`、`127.0.0.1`）。部署后请在 AstrBot 插件配置里改成你自己的。

## 记忆与关系

| 插件 | 说明 |
|---|---|
| `astrbot_plugin_livingmemory` | 长期记忆：证据引文、置信度、事件时间线、实体/时间/情绪召回、冲突复核、关系状态。**AGPL-3.0**，上游 lxfight。 |
| `astrbot_plugin_memory_sync` | 双端记忆/会话同步（手动或定时，服务端权威合并）。 |

## 主动与对话

| 插件 | 说明 |
|---|---|
| `astrbot_plugin_dafeiyu_proactive_tg` | 仅主人私聊的主动消息；真实素材 + 确定性门控 + LLM 门控。 |
| `astrbot_plugin_dafeiyu_backfill` | 离线补回复：把漏掉的用户消息合并成一段自然回复。 |
| `astrbot_plugin_dafeiyu_gate` | 陌生人闸：Telegram 私聊 24h 只回一次，不写历史、不召回记忆。 |
| `astrbot_plugin_tg_webchat_mirror` | Telegram 与 WebChat 会话互通镜像。 |
| `astrbot_plugin_context_fix` / `_local` | 历史图片/思考块清理；云端与本地模型两套压缩策略。 |

## 语音与形象

| 插件 | 说明 |
|---|---|
| `astrbot_plugin_dafeiyu_voice_tool` | `send_voice` 工具、长语音/整段模式、QQ SILK 转码。 |
| `astrbot_plugin_dafeiyu_voice_switch` | 按轮切换音色，注入 `voice_id + instruction`。 |
| `astrbot_plugin_dafeiyu_live2d` | 聊天结果推送到鲸语舞台 / Live2D 桌宠。 |
| `astrbot_plugin_dafeiyu_embed` | 守护本机 llama.cpp bge-m3 向量服务（127.0.0.1:11434）。 |

## 内容与生活

| 插件 | 说明 |
|---|---|
| `astrbot_plugin_dafeiyu_bili` | B 站：搜索、视频信息、字幕/ASR、弹幕、热评、草稿式写操作。 |
| `astrbot_plugin_bili_guard` | 给 B 站工具注入主人身份与提示词。 |
| `astrbot_plugin_dafeiyu_web` | 联网搜索与链接解析（B 站/抖音/贴吧/小黑盒）。 |
| `astrbot_plugin_dafeiyu_video` | 视频/图片理解（本机 → 云端逐级兜底）。 |
| `astrbot_plugin_dafeiyu_music` | 听歌与翻唱（MOSS-Audio + Demucs + Seed-VC）。 |
| `astrbot_plugin_dafeiyu_health` | 小米健康数据、关怀提醒、跑步账本。 |

## 运维

| 插件 | 说明 |
|---|---|
| `astrbot_plugin_dafeiyu_backup` | 百度网盘 xpan 备份：核心包每日、全量包每周；回读校验。 |

## 未收录的第三方插件

`group_chat_plus`、`get_px`、`meme_manager`、`qzone_ultra`、`parser`、`qqprofile`、`cost_control`、`mihome`、`stealer`、`temp_block`、`baidu_curl`、`anima_master`、`qq_tools`、`polite_silence` 等由社区维护，请从各自仓库安装；本仓库不分发其代码。许可信息见根目录 `NOTICE.md`。

## 安装

```bash
cp -r plugins/<插件名> <AstrBot>/data/plugins/
# 重启 AstrBot，在后台填写配置
```
