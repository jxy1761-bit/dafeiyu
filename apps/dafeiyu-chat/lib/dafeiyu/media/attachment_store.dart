import 'dart:typed_data';

import '../../astrbot/astrbot_client.dart';

/// 附件字节缓存。
///
/// 消息里只有 attachment_id,真正的图 / 语音 / 文件走
/// `get_attachment` 拉一次,进程内记住。和 dsh-Go 那边
/// `imageProviderBytes` 的作用一样:列表滚动时不至于每帧重下。
class Attachments {
  static AstrBotClient? client;

  static final Map<String, Uint8List> _cache = {};

  static Uint8List? peek(String id) => _cache[id];

  static Future<Uint8List> load(String id) async {
    final hit = _cache[id];
    if (hit != null) return hit;
    final c = client;
    if (c == null) throw StateError('还没连上服务器');
    final bytes = await c.attachmentBytes(id);
    _cache[id] = bytes;
    return bytes;
  }

  /// 本地先塞一份(刚发出去的图,不用再从服务器拉回来)。
  static void put(String id, Uint8List bytes) => _cache[id] = bytes;

  static void clear() => _cache.clear();
}
