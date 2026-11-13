# 大肥鱼 · 基础版（Android）

局域网瘦客户端：连接另一台设备上的 AstrBot WebChat，界面仿 Telegram，支持流式回复、图片、语音、文件与共享会话。

## 功能

- 连接设置：地址自动发现（局域网扫描）、共享/隔离会话
- SSE 流式回复与断线重连
- 图片、语音、文件收发与下载
- 省电模式（冻结动态表情）与 60/120Hz 高刷
- 完整版在线时的实时同步（Hub）

## 构建

```bash
flutter pub get
flutter build apk --release --target-platform android-arm64
```

## 说明

- 需要服务端已开启 WebChat 平台；完整版默认监听 `6185`。
- 若在另一台设备上使用，请让服务端允许局域网访问。

## 致谢

- [dsh-Go](https://github.com/ccch1mneyyy/dsh-Go)（AGPL-3.0，UI 框架与设计体系）
- [PiPilot](https://github.com/ccch1mneyyy/pi_pilot)（MIT，UI 通用组件）
- [AstrBot](https://github.com/AstrBotDevs/AstrBot)（AGPL-3.0）
- 完整版宿主基于 [AstrBot for Android](https://github.com/JunWan666/astrbot-for-android)（MIT）

本 App 代码按 AGPL-3.0 提供；详见仓库根目录 `NOTICE.md` 与 `LICENSE`。
