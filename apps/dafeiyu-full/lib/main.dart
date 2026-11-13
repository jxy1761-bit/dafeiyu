import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'dafeiyu/app.dart';
import 'dafeiyu/notifications.dart';
import 'dafeiyu/power_save.dart';
import 'dafeiyu/prefs.dart';
import 'full/full_bridge.dart';

/// 大肥鱼：局域网聊天客户端。
///
/// 界面骨架、主题、动效 token、按压反馈、连续圆角全部来自 dsh-Go 的设计系统，
/// 业务只有一件事——把话送进 AstrBot 的 webchat，把流接回来。
Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Prefs.load();
  PowerSave.load();
  unawaited(FullBridge.setFrameRate(Prefs.i.frameRate));
  SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
  SystemChrome.setSystemUIOverlayStyle(
    const SystemUiOverlayStyle(
      statusBarColor: Colors.transparent,
      statusBarIconBrightness: Brightness.light,
      systemNavigationBarColor: Color(0xFF17212B),
    ),
  );
  runApp(const DafeiyuApp());
  // 等首帧出来再要通知权限，避免启动瞬间弹窗盖住界面。
  unawaited(Future<void>.delayed(
    const Duration(milliseconds: 900),
    MessageNotifications.requestPermission,
  ));
  unawaited(_autoStartLocal());
}

/// 完整版开机即拉起本机 AstrBot；基础版没有这个通道，会自动跳过。
Future<void> _autoStartLocal() async {
  await FullBridge.ensureLocal();
}
