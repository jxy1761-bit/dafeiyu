import 'dart:typed_data';

/// 一条消息里的一个片段。和 AstrBot 的 message parts 一一对应:
/// `plain` / `image` / `record` / `file`。
///
/// 媒体片段只带 attachment_id —— 真正的字节走
/// `GET /api/chat/get_attachment?attachment_id=…`,和 dsh-Go 里
/// `ImageAttachment.attachmentId` / `FileAttachment.attachmentId` 同一个思路:
/// 消息里只存引用,不把 base64 塞进历史(那会把上下文撑爆,QQ 那边踩过)。
sealed class MsgPart {
  const MsgPart();

  Map<String, dynamic> toJson();
}

class TextPart extends MsgPart {
  const TextPart(this.text);
  final String text;

  @override
  Map<String, dynamic> toJson() => {'type': 'plain', 'text': text};
}

enum MediaKind { image, record, file }

/// 图片 / 语音 / 文件。语音也是 attachment,只是 type=record。
class MediaPart extends MsgPart {
  const MediaPart({
    required this.kind,
    required this.attachmentId,
    this.name,
    this.bytes,
    this.mime,
    this.width,
    this.height,
    this.duration,
  });

  final MediaKind kind;
  final String attachmentId;
  final String? name;
  final int? bytes;
  final String? mime;
  final int? width;
  final int? height;

  /// 语音时长(秒);服务端不一定给,拿不到就按 4.6 字/秒估。
  final double? duration;

  bool get isImage => kind == MediaKind.image;
  bool get isVoice => kind == MediaKind.record;
  bool get isFile => kind == MediaKind.file;

  MediaPart copyWith({int? width, int? height, double? duration}) => MediaPart(
        kind: kind,
        attachmentId: attachmentId,
        name: name,
        bytes: bytes,
        mime: mime,
        width: width ?? this.width,
        height: height ?? this.height,
        duration: duration ?? this.duration,
      );

  @override
  Map<String, dynamic> toJson() => {
        'type': switch (kind) {
          MediaKind.image => 'image',
          MediaKind.record => 'record',
          MediaKind.file => 'file',
        },
        'attachment_id': attachmentId,
        if (name != null && name!.isNotEmpty) 'filename': name,
      };
}

/// 上传完等发送的草稿:还没分配到 attachment_id 时先拿本地字节预览。
class DraftPart {
  const DraftPart({
    required this.bytes,
    required this.filename,
    required this.kind,
    this.mime,
    this.duration,
  });

  final Uint8List bytes;
  final String filename;
  final MediaKind kind;
  final String? mime;
  final double? duration;
}
