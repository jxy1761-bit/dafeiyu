import 'dart:async';
import 'dart:typed_data';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/material.dart';

import '../../astrbot/models.dart';
import '../../ui/common/frozen_image.dart';
import '../../ui/common/pressable.dart';
import '../../ui/theme/theme.dart';
import 'attachment_store.dart';
import 'downloads.dart';
import 'image_viewer.dart';

/// 图片消息。圆角 14,dsh-Go 那边图片也走 `AppShape.md` 这一档。
///
/// 点开全屏(可双指缩放),GIF 直接在气泡里逐帧播放。
class MediaImage extends StatelessWidget {
  const MediaImage({
    super.key,
    required this.part,
    this.maxWidth = 240,
    this.maxHeight = 300,
    this.local,
  });

  final MediaPart part;
  final double maxWidth;
  final double maxHeight;

  /// 本地已有字节(刚选的图,还没上传完)
  final Uint8List? local;

  /// 长按:存进系统下载目录。和文件卡走同一条通道。
  Future<void> _save(BuildContext context) async {
    final messenger = ScaffoldMessenger.of(context);
    final name = part.name ?? 'image_${DateTime.now().millisecondsSinceEpoch}.png';
    try {
      final bytes = local ?? await Attachments.load(part.attachmentId);
      final where = await Downloads.save(bytes: bytes, name: name);
      messenger
        ..hideCurrentSnackBar()
        ..showSnackBar(
          SnackBar(content: Text('已保存到 $where'), duration: const Duration(seconds: 3)),
        );
    } catch (e) {
      messenger
        ..hideCurrentSnackBar()
        ..showSnackBar(SnackBar(content: Text('保存失败：$e')));
    }
  }

  @override
  Widget build(BuildContext context) {
    return Pressable(
      onTap: () => openImageViewer(context, part, local: local),
      onLongPress: () => _save(context),
      child: FutureBuilder<Uint8List>(
        future: local != null ? Future.value(local) : Attachments.load(part.attachmentId),
        builder: (context, snap) {
          if (snap.hasError) {
            return _Broken(width: maxWidth * 0.6, height: 72);
          }
          final bytes = snap.data;
          if (bytes == null) {
            return _Loading(width: maxWidth * 0.7, height: 120);
          }
          return ClipRRect(
            borderRadius: BorderRadius.circular(AppShape.md),
            child: ConstrainedBox(
              constraints: BoxConstraints(maxWidth: maxWidth, maxHeight: maxHeight),
              child: FrozenImage(bytes: bytes, fit: BoxFit.cover),
            ),
          );
        },
      ),
    );
  }
}

class _Loading extends StatelessWidget {
  const _Loading({required this.width, required this.height});
  final double width;
  final double height;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: width,
      height: height,
      decoration: BoxDecoration(
        color: Theme.of(context).colorScheme.onSurface.withValues(alpha: 0.06),
        borderRadius: BorderRadius.circular(AppShape.md),
      ),
      child: Center(
        child: SizedBox.square(
          dimension: 18,
          child: CircularProgressIndicator(
            strokeWidth: 2,
            color: DshColors.of(context).textTertiary,
          ),
        ),
      ),
    );
  }
}

class _Broken extends StatelessWidget {
  const _Broken({required this.width, required this.height});
  final double width;
  final double height;

  @override
  Widget build(BuildContext context) {
    final dsh = DshColors.of(context);
    return Container(
      width: width,
      height: height,
      decoration: BoxDecoration(
        color: Theme.of(context).colorScheme.onSurface.withValues(alpha: 0.05),
        borderRadius: BorderRadius.circular(AppShape.md),
      ),
      child: Center(
        child: Icon(AppIcons.warning, size: 18, color: dsh.textTertiary),
      ),
    );
  }
}

/// 语音消息:一颗播放键 + 一根进度条 + 时长。
///
/// 语音也是 attachment(服务端 type=record)。没有现成的波形数据,所以画
/// 一排等高的小竖条当"节拍",播放时按进度点亮。
class VoiceBubble extends StatefulWidget {
  const VoiceBubble({
    super.key,
    required this.part,
    this.mine = false,
    this.local,
  });

  final MediaPart part;
  final bool mine;
  final Uint8List? local;

  @override
  State<VoiceBubble> createState() => _VoiceBubbleState();
}

class _VoiceBubbleState extends State<VoiceBubble> {
  static final _player = AudioPlayer();
  static StreamSubscription<Duration>? _posSub;
  static StreamSubscription<void>? _doneSub;
  static String? _playingId;

  bool get _isPlaying => _playingId == widget.part.attachmentId;
  Duration _pos = Duration.zero;
  Duration _total = Duration.zero;

  @override
  void initState() {
    super.initState();
    // 时长:服务端不给就按字节估(WAV 是 44 字节头 + 采样)。
    final bytes = widget.part.bytes;
    if (bytes != null && bytes > 44) {
      _total = Duration(milliseconds: ((bytes - 44) / 32000 * 1000).round());
    }
  }

  @override
  void dispose() {
    // 播放器是共享的,这里只解绑本地订阅
    if (_isPlaying) _stop();
    super.dispose();
  }

  Future<void> _toggle() async {
    if (_isPlaying) {
      await _player.pause();
      setState(() => _playingId = null);
      return;
    }
    final bytes = widget.local ?? await Attachments.load(widget.part.attachmentId);
    await _player.stop();
    _posSub?.cancel();
    _doneSub?.cancel();
    _posSub = _player.onPositionChanged.listen((d) {
      if (mounted) setState(() => _pos = d);
    });
    _doneSub = _player.onPlayerComplete.listen((_) {
      if (mounted) {
        setState(() {
          _playingId = null;
          _pos = Duration.zero;
        });
      }
    });
    await _player.play(BytesSource(bytes));
    final dur = await _player.getDuration();
    if (mounted && dur != null) setState(() => _total = dur);
    setState(() => _playingId = widget.part.attachmentId);
  }

  Future<void> _stop() async {
    await _player.stop();
    _playingId = null;
  }

  String _fmt(Duration d) {
    final s = d.inSeconds;
    return '${s ~/ 60}:${(s % 60).toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    final mine = widget.mine;
    final surfaces = DshSurfaces.of(context);
    final fg = mine ? surfaces.onUserBubble : Theme.of(context).colorScheme.onSurface;
    final total = _total.inMilliseconds == 0 ? 1.0 : _total.inMilliseconds.toDouble();
    final progress = _isPlaying
        ? (_pos.inMilliseconds / total).clamp(0.0, 1.0)
        : 0.0;

    return Pressable(
      onTap: _toggle,
      child: Container(
        width: 190,
        padding: const EdgeInsets.fromLTRB(8, 8, 14, 8),
        decoration: ShapeDecoration(
          color: mine
              ? Colors.white.withValues(alpha: 0.10)
              : Theme.of(context).colorScheme.onSurface.withValues(alpha: 0.06),
          shape: const StadiumBorder(),
        ),
        child: Row(
          children: [
            Container(
              width: 30,
              height: 30,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: mine ? surfaces.onUserBubble.withValues(alpha: 0.16) : null,
                border: mine ? null : Border.all(color: fg.withValues(alpha: 0.25)),
              ),
              child: Icon(
                _isPlaying ? AppIcons.pause : AppIcons.play,
                size: 16,
                color: fg,
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: SizedBox(
                height: 20,
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.center,
                  children: [
                    for (var i = 0; i < 18; i++)
                      Expanded(
                        child: Padding(
                          padding: const EdgeInsets.symmetric(horizontal: 1),
                          child: Container(
                            height: 6.0 + ((i * 7) % 11),
                            decoration: BoxDecoration(
                              color: fg.withValues(
                                alpha: i / 18 < progress ? 0.95 : 0.32,
                              ),
                              borderRadius: BorderRadius.circular(2),
                            ),
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ),
            const SizedBox(width: 10),
            Text(
              _fmt(_isPlaying ? _pos : _total),
              style: Theme.of(context).textTheme.labelSmall?.copyWith(color: fg.withValues(alpha: 0.8)),
            ),
          ],
        ),
      ),
    );
  }
}

/// 文件消息:图标 + 文件名 + 大小,点了存到应用目录并提示路径。
class FileCard extends StatelessWidget {
  const FileCard({super.key, required this.part, this.mine = false});

  final MediaPart part;
  final bool mine;

  static const _extIcon = {
    'zip': AppIcons.files,
    'txt': AppIcons.file,
    'md': AppIcons.file,
  };

  Future<void> _save(BuildContext context) async {
    final messenger = ScaffoldMessenger.of(context);
    final name = (part.name ?? 'file.bin').split('/').last;
    try {
      final bytes = await Attachments.load(part.attachmentId);
      // 存进系统「下载」目录,不是应用私有目录
      final where = await Downloads.save(
        bytes: bytes,
        name: name,
        mime: part.mime ?? Downloads.mimeOf(name),
      );
      messenger
        ..hideCurrentSnackBar()
        ..showSnackBar(
          SnackBar(content: Text('已保存到 $where'), duration: const Duration(seconds: 3)),
        );
    } catch (e) {
      messenger
        ..hideCurrentSnackBar()
        ..showSnackBar(SnackBar(content: Text('保存失败：$e')));
    }
  }

  String get _size {
    final b = part.bytes ?? 0;
    if (b <= 0) return '';
    if (b < 1024) return '$b B';
    if (b < 1024 * 1024) return '${(b / 1024).toStringAsFixed(1)} KB';
    return '${(b / 1024 / 1024).toStringAsFixed(1)} MB';
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final surfaces = DshSurfaces.of(context);
    final fg = mine ? surfaces.onUserBubble : theme.colorScheme.onSurface;
    final name = part.name ?? '文件';
    final ext = name.contains('.') ? name.split('.').last.toLowerCase() : '';
    return Pressable(
      onTap: () => _save(context),
      child: Container(
        width: 220,
        padding: const EdgeInsets.all(10),
        decoration: ShapeDecoration(
          color: mine
              ? Colors.white.withValues(alpha: 0.10)
              : theme.colorScheme.onSurface.withValues(alpha: 0.06),
          shape: AppShape.bubble,
        ),
        child: Row(
          children: [
            Container(
              width: 34,
              height: 34,
              decoration: BoxDecoration(
                color: fg.withValues(alpha: 0.12),
                borderRadius: BorderRadius.circular(AppShape.sm),
              ),
              child: Icon(_extIcon[ext] ?? AppIcons.file, size: 18, color: fg),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    name,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: theme.textTheme.bodySmall?.copyWith(color: fg),
                  ),
                  if (_size.isNotEmpty)
                    Text(
                      _size,
                      style: theme.textTheme.labelSmall?.copyWith(
                        color: fg.withValues(alpha: 0.7),
                      ),
                    ),
                ],
              ),
            ),
            Icon(AppIcons.download, size: 16, color: fg.withValues(alpha: 0.8)),
          ],
        ),
      ),
    );
  }
}
