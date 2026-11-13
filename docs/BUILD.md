# 构建指南

## 环境

- Flutter 3.47+ / Dart 3.13+
- JDK 17
- Android SDK（compileSdk 36，build-tools 34+）
- 建议使用 ASCII 路径构建（中文路径可能导致 Gradle/Kotlin 增量编译异常）

## 完整版

```bash
cd apps/dafeiyu-full
export JAVA_HOME="<JDK 17>"
export ANDROID_HOME="<Android SDK>"
flutter pub get
flutter build apk --release --target-platform android-arm64
```

产物：`build/app/outputs/flutter-apk/app-release.apk`。

说明：

- `android/manager` 为 Kotlin/Compose 宿主模块，基于 AstrBot for Android 改造。
- 运行环境与 AstrBot 本体不随源码仓库分发，首次启动时由 App 初始化/下载。
- 发布请使用你自己的签名证书；仓库默认使用 debug 签名仅供本地测试。

## 基础版

```bash
cd apps/dafeiyu-chat
flutter pub get
flutter build apk --release --target-platform android-arm64
```

## 常见问题

- **release 包无法联网**：确认 `android/app/src/main/AndroidManifest.xml` 含 `INTERNET` 与 `usesCleartextTraffic`。
- **无法扫描到服务器**：两台设备需在同一网段；服务端需允许局域网访问。
- **中文路径构建失败**：把工程放到纯英文路径，或使用目录联接。
- **高刷不生效**：先在系统显示设置把刷新率设为 120Hz；App 只负责向系统请求。
