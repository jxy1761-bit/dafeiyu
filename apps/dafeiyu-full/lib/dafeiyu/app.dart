import 'package:flutter/material.dart';

import '../ui/theme/app_theme.dart';
import 'chat/chat_screen.dart';

/// 大肥鱼的 App 壳。主题直接用 dsh-Go 那套（配色 / 圆角 / 动效 / 字体），
/// 页面骨架也照它的聊天页搭，只跟 AstrBot 说话。
class DafeiyuApp extends StatelessWidget {
  const DafeiyuApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: '大肥鱼',
      debugShowCheckedModeBanner: false,
      theme: buildDarkTheme(),
      darkTheme: buildDarkTheme(),
      themeMode: ThemeMode.dark,
      home: const ChatScreen(),
    );
  }
}
