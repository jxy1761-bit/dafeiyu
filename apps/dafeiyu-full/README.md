# 大肥鱼 · 完整版（Android）

内置 AstrBot 运行环境的 Android 宿主 + Flutter 聊天客户端。单机即可运行，也可作为基础版的服务器。

## 功能

- PRoot Ubuntu 运行环境，一键初始化/启动/停止 AstrBot
- 原生 Compose 管理界面（服务、日志、备份、SSH、局域网）
- Flutter 聊天界面：SSE 流式回复、图片/语音/文件、表情、共享会话
- WebChat Hub（实时同步）与 WebUI 入口
- 性能设置：省电模式（冻结动态表情）、60/120Hz 高刷
- 与 `plugins/` 中的记忆、主动、语音、B 站等插件配合使用

## 技术栈

- Flutter 3.47 / Dart 3.13
- Kotlin + Jetpack Compose（`android/manager`，基于 AstrBot for Android 改造）
- AstrBot 4.29.x（运行时由用户自行安装/初始化）

## 构建

```bash
export JAVA_HOME="<你的 JDK 17>"
export ANDROID_HOME="<你的 Android SDK>"
flutter pub get
flutter build apk --release --target-platform android-arm64
```

产物：`build/app/outputs/flutter-apk/app-release.apk`。

## 配置

- 服务器地址、账号、密码在 App 的「连接设置」中填写。
- 首次启动会自动初始化 PRoot 运行环境并拉起 AstrBot。
- 备份/恢复、SSH、局域网访问在「服务管理」中操作。

## 致谢

- [AstrBot for Android](https://github.com/JunWan666/astrbot-for-android)（MIT）
- [dsh-Go](https://github.com/ccch1mneyyy/dsh-Go)（AGPL-3.0，UI 框架与设计体系）
- [PiPilot](https://github.com/ccch1mneyyy/pi_pilot)（MIT，UI 通用组件）
- [AstrBot](https://github.com/AstrBotDevs/AstrBot)（AGPL-3.0）

本 App 代码按 AGPL-3.0 提供；详见仓库根目录 `NOTICE.md` 与 `LICENSE`。
