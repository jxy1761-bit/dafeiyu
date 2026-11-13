import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';

import '../../dafeiyu/power_save.dart';

/// 图片显示：省电模式开启时只解码并绘制首帧，避免动态 WebP/GIF 持续解码重绘。
class FrozenImage extends StatefulWidget {
  const FrozenImage({super.key, required this.bytes, this.fit = BoxFit.cover});

  final Uint8List bytes;
  final BoxFit fit;

  @override
  State<FrozenImage> createState() => _FrozenImageState();
}

class _FrozenImageState extends State<FrozenImage> {
  ui.Image? _first;
  bool _decoding = false;

  @override
  void initState() {
    super.initState();
    if (PowerSave.enabled.value) _scheduleDecode();
  }

  void _scheduleDecode() {
    WidgetsBinding.instance.addPostFrameCallback((_) => _decode());
  }

  Future<void> _decode() async {
    if (_decoding || _first != null) return;
    _decoding = true;
    try {
      final codec = await ui.instantiateImageCodec(widget.bytes);
      final frame = await codec.getNextFrame();
      if (!mounted) {
        frame.image.dispose();
        return;
      }
      setState(() => _first = frame.image);
    } catch (_) {
      // 解码失败就退回原来的 Image.memory。
    } finally {
      _decoding = false;
    }
  }

  @override
  void dispose() {
    _first?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<bool>(
      valueListenable: PowerSave.enabled,
      builder: (context, on, _) {
        if (!on) {
          return Image.memory(widget.bytes, fit: widget.fit, gaplessPlayback: true);
        }
        final image = _first;
        if (image == null) {
          _scheduleDecode();
          return Image.memory(widget.bytes, fit: widget.fit, gaplessPlayback: true);
        }
        return RawImage(image: image, fit: widget.fit);
      },
    );
  }
}
