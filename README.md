# 大肥鱼 · AI 陪伴机器人

一个以「长期记忆 + 主动陪伴」为核心的 QQ / Telegram / WebChat 机器人，以及配套的 Android 全功能宿主与 Flutter 聊天客户端。

> 本仓库只包含**代码与配置模板**，不包含任何聊天记录、数据库、账号凭据、语音样本或角色美术资源。运行所需密钥、Cookie、人设素材请放在本地 `private/` 目录，不要提交。

---

## 项目组成

| 目录 | 内容 |
|---|---|
| `apps/dafeiyu-full/` | 完整版 Android App：内置 PRoot Ubuntu + AstrBot 运行管理（Kotlin/Compose）+ Flutter 聊天界面 + 实时同步 Hub |
| `apps/dafeiyu-chat/` | 基础版 Android App：局域网瘦客户端，连接另一台设备上的 AstrBot |
| `plugins/` | 自研 AstrBot 插件（记忆、主动找人、语音、B 站、备份、健康、陪看等） |
| `config-pack/` | 配置导入脚本与参数模板（人设模板不随仓库提供，请自备） |
| `docs/` | 架构、构建、更新记录与隐私说明 |

主要能力：

- **长期记忆**：LivingMemory 2.9.x，证据引文、置信度、事件时间线、实体/时间/情绪多路召回、冲突复核、关系连续性。
- **主动陪伴**：主人私聊主动消息，真实素材门控 + LLM 上下文门控，冷却与退避。
- **语音**：Breeze / MiMo / Edge 多 TTS，按平台转 SILK / OGG；语音识别可接 MiMo 或本机 ASR。
- **内容理解**：B 站视频解析、弹幕/热评、看图、陪看、联网搜索。
- **生活数据**：小米运动健康、跑步账本（iGPSPORT / Strava 预留）。
- **可靠运维**：SQLite 在线快照、百度网盘 xpan 备份、恢复指南、单实例锁与回读校验。
- **移动端体验**：60/120Hz 刷新率选择、省电模式（冻结动态表情）、Live2D 桌宠桥接。

---

## 快速开始

1. 安装 [AstrBot](https://github.com/AstrBotDevs/AstrBot)（4.29.x）。
2. 把 `plugins/` 下需要的插件复制到 AstrBot 的 `data/plugins/`。
3. 参考 `config-pack/README.md` 导入配置模板（人设请使用你自己的角色）。
4. 在 AstrBot 后台配置模型、平台适配器（QQ / Telegram / WebChat）与 Embedding 服务。
5. Android 端任选其一：
   - 直接构建/安装 `apps/dafeiyu-full`（自带运行环境，适合单机部署）；
   - 或安装 `apps/dafeiyu-chat`，连接到已有 AstrBot。

构建步骤见 [`docs/BUILD.md`](docs/BUILD.md)。预构建 APK 见 [Releases](https://github.com/jxy1761-bit/dafeiyu/releases)。

---

## 致敬与致谢

这个 App 的界面与 Android 宿主能走到今天，直接受益于以下开源项目。**特别感谢原作者与所有贡献者**：

### 1. AstrBot for Android

- 作者：**JunWan666**
- 仓库：<https://github.com/JunWan666/astrbot-for-android>
- 许可：MIT
- 本项目 `apps/dafeiyu-full/android/manager` 基于该项目改造（PRoot 运行环境、服务管理、WebUI、SSH、备份等），并做了面向本项目的裁剪与扩展。

### 2. dsh-Go（DeepSeek Harness）

- 作者：**CikeSeven（ccch1mneyyy）**
- 仓库：<https://github.com/ccch1mneyyy/dsh-Go>
- 许可：AGPL-3.0
- 本项目 App 的 **UI 框架与设计体系**（配色、圆角、玻璃质感、动效 token、排版、聊天页布局与交互范式）参考/改编自 dsh-Go；其中部分通用组件同时源自同作者的 PiPilot。

### 3. PiPilot

- 作者：**CikeSeven（ccch1mneyyy）**
- 仓库：<https://github.com/ccch1mneyyy/pi_pilot>
- 许可：MIT
- 本项目的界面骨架、主题、动效 token、玻璃质感与部分通用组件（状态点、输入指示、流式光标等）参考/改编自该项目。

完整的第三方许可与致谢见 [`NOTICE.md`](NOTICE.md)。

---

## 安全与隐私

- 仓库内**不含**任何真实密钥、Token、Cookie、QQ/Telegram 账号、聊天记录、日记、健康数据或语音样本。
- 所有敏感配置通过 `private/`、环境变量或本机后台填写；`.gitignore` 已默认忽略。
- 请不要把 `private/`、`data/`、数据库、日志或截图提交到公开仓库。
- 角色形象、Live2D 模型、音色与表情包**未随仓库分发**，请使用你有权使用的素材。

---

## 许可

- 本仓库整体：**GNU AGPL-3.0**（见 `LICENSE`）。原因：App 的 UI 参考了 AGPL-3.0 的 **dsh-Go**，并包含 AGPL-3.0 的 LivingMemory。
- `config-pack/LICENSE` 等第三方/独立子目录保留其原有许可。
- 引用的第三方项目遵循各自许可，详见 `NOTICE.md`。

本项目仅供学习与个人使用。使用 QQ / Telegram / B 站 / 小米健康等平台时，请遵守对应平台的服务条款；由部署者自行承担账号与合规责任。
