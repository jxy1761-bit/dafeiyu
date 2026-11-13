import 'dart:typed_data';

import 'package:flutter/services.dart';

/// 存到系统「下载」目录。
///
/// 原生侧(Android)走 MediaStore.Downloads,文件在文件管理器里能看到,
/// 卸载 App 也不会被一起删掉。返回一个给人看的位置,例如
/// `Download/大肥鱼/说明.txt`。
class Downloads {
  const Downloads._();

  static const _channel = MethodChannel('dafeiyu/downloads');

  static String mimeOf(String name) {
    final ext = name.contains('.') ? name.split('.').last.toLowerCase() : '';
    return switch (ext) {
      'png' => 'image/png',
      'jpg' || 'jpeg' => 'image/jpeg',
      'gif' => 'image/gif',
      'webp' => 'image/webp',
      'pdf' => 'application/pdf',
      'txt' || 'md' => 'text/plain',
      'json' => 'application/json',
      'zip' => 'application/zip',
      'mp3' => 'audio/mpeg',
      'wav' => 'audio/wav',
      'mp4' => 'video/mp4',
      _ => 'application/octet-stream',
    };
  }

  static Future<String> save({
    required Uint8List bytes,
    required String name,
    String? mime,
  }) async {
    final where = await _channel.invokeMethod<String>('save', {
      'name': name,
      'mime': mime ?? mimeOf(name),
      'bytes': bytes,
    });
    return where ?? 'Download/大肥鱼/$name';
  }
}
