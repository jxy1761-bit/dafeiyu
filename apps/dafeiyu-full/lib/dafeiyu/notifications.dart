import 'package:flutter/services.dart';

/// 系统消息通知。
///
/// Android 13+ 需要 POST_NOTIFICATIONS 授权；后台收消息由原生前台服务完成，
/// 不依赖 Flutter 在后台继续运行。
class MessageNotifications {
  static const _channel = MethodChannel('dafeiyu/notifications');
  static bool _asked = false;

  static Future<void> requestPermission() async {
    if (_asked) return;
    _asked = true;
    try {
      await _channel.invokeMethod('requestPermission');
    } catch (_) {
      // 旧系统/非 Android 环境没有这个通道，忽略。
    }
  }

  /// 启动原生消息守护：登录 AstrBot 并订阅 Hub，App 不在前台时弹通知。
  static Future<void> startWatch({
    required String baseUrl,
    required String hubUrl,
    required String username,
    required String password,
    required String sessionId,
  }) async {
    if (baseUrl.trim().isEmpty ||
        hubUrl.trim().isEmpty ||
        username.trim().isEmpty ||
        password.isEmpty) {
      await stopWatch();
      return;
    }
    try {
      await _channel.invokeMethod('startWatch', {
        'baseUrl': baseUrl,
        'hubUrl': hubUrl,
        'username': username,
        'password': password,
        'sessionId': sessionId,
      });
    } catch (_) {
      // 通知服务启动失败不能影响聊天。
    }
  }

  static Future<void> stopWatch() async {
    try {
      await _channel.invokeMethod('stopWatch');
    } catch (_) {}
  }
}
