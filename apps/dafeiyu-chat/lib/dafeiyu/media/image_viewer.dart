import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../../astrbot/models.dart';
import '../../ui/common/pressable.dart';
import '../../ui/theme/theme.dart';
import 'attachment_store.dart';

/// 点开图片:全屏、可双指缩放、可拖走的查看器。
///
/// 和 dsh-Go 的 `media_viewer` 同一个角色(它那边还有视频/多图翻页,
/// 这里先只做单图)。
Future<void> openImageViewer(
  BuildContext context,
  MediaPart part, {
  Uint8List? local,
}) {
  return Navigator.of(context).push(
    PageRouteBuilder<void>(
      opaque: false,
      barrierColor: Colors.black,
      transitionDuration: AppMotion.of(context, AppMotion.quick),
      reverseTransitionDuration: AppMotion.of(context, AppMotion.quick),
      pageBuilder: (_, __, ___) => _ImageViewer(part: part, local: local),
      transitionsBuilder: (context, anim, second, child) =>
          FadeTransition(opacity: anim, child: child),
    ),
  );
}

class _ImageViewer extends StatelessWidget {
  const _ImageViewer({required this.part, this.local});

  final MediaPart part;
  final Uint8List? local;

  @override
  Widget build(BuildContext context) {
    final top = MediaQuery.paddingOf(context).top;
    return Scaffold(
      backgroundColor: Colors.black,
      body: Stack(
        children: [
          Positioned.fill(
            child: GestureDetector(
              onTap: () => Navigator.of(context).maybePop(),
              child: FutureBuilder<Uint8List>(
                future: local != null ? Future.value(local) : Attachments.load(part.attachmentId),
                builder: (context, snap) {
                  final bytes = snap.data;
                  if (bytes == null) {
                    return const Center(
                      child: SizedBox.square(
                        dimension: 26,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      ),
                    );
                  }
                  return InteractiveViewer(
                    minScale: 1,
                    maxScale: 4,
                    child: Center(child: Image.memory(bytes, fit: BoxFit.contain)),
                  );
                },
              ),
            ),
          ),
          Positioned(
            top: top + 8,
            right: 12,
            child: SizedBox.square(
              dimension: 40,
              child: Pressable(
                onTap: () => Navigator.of(context).maybePop(),
                child: GlassSurface(
                  tier: GlassTier.chrome,
                  circle: true,
                  child: const Center(
                    child: Icon(AppIcons.close, size: 20, color: Colors.white),
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
