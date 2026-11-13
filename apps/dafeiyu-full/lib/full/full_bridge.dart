import 'package:flutter/services.dart';

/// 完整版宿主桥。基础版里通道不存在，所有方法都会安全地返回 null/false。
class FullBridge {
  static const MethodChannel _channel = MethodChannel('dafeiyu/full');

  static Future<Map<String, dynamic>?> localStatus() async {
    try {
      return await _channel.invokeMapMethod<String, dynamic>('localStatus');
    } on MissingPluginException {
      return null;
    } on PlatformException {
      return null;
    }
  }

  static Future<bool> startLocal() => _invoke('startLocal');

  /// 完整版打开时调用：只要运行环境已装好就让 AstrBot 起来，重复调用无副作用。
  static Future<bool> ensureLocal() => _invoke('ensureLocal');

  static Future<bool> stopLocal() => _invoke('stopLocal');

  static Future<bool> initialize() => _invoke('initialize');

  static Future<bool> openManager() => _invoke('openManager');

  static Future<bool> setLan(bool enabled) =>
      _invoke('setLan', <String, dynamic>{'enabled': enabled});

  /// 请求界面帧率（60/120）。不支持时原生侧会自动选最接近的显示模式。
  static Future<bool> setFrameRate(int rate) =>
      _invoke('setFrameRate', <String, dynamic>{'rate': rate});

  static Future<String?> hubUrl() async {
    try {
      return await _channel.invokeMethod<String>('hubUrl');
    } on MissingPluginException {
      return null;
    } on PlatformException {
      return null;
    }
  }

  static Future<bool> _invoke(String method, [Map<String, dynamic>? args]) async {
    try {
      final result = await _channel.invokeMethod<bool>(method, args);
      return result == true;
    } on MissingPluginException {
      return false;
    } on PlatformException {
      return false;
    }
  }
}
