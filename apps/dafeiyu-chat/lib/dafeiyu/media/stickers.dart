import 'dart:io';
import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';

import '../../ui/common/pressable.dart';
import '../../ui/theme/theme.dart';

/// 表情包库:本机目录里的一堆图(含 gif)。
///
/// 没有独立的"表情"消息类型 —— AstrBot 那边图片就是一种消息,所以表情包
/// 发送等同于发一张图,只是来源是这里的文件。
class StickerStore {
  static Directory? _dir;

  static Future<Directory> dir() async {
    final cached = _dir;
    if (cached != null) return cached;
    final docs = await getApplicationDocumentsDirectory();
    final d = Directory('${docs.path}/stickers');
    if (!await d.exists()) await d.create(recursive: true);
    _dir = d;
    return d;
  }

  static Future<List<File>> list() async {
    final d = await dir();
    final files = await d
        .list()
        .where((e) => e is File)
        .cast<File>()
        .toList();
    files.sort((a, b) => b.path.compareTo(a.path));
    return files
        .where((f) {
          final p = f.path.toLowerCase();
          return p.endsWith('.png') ||
              p.endsWith('.jpg') ||
              p.endsWith('.jpeg') ||
              p.endsWith('.gif') ||
              p.endsWith('.webp');
        })
        .toList();
  }

  /// 从相册/文件里导入若干张。返回导入成功的文件。
  static Future<List<File>> import() async {
    final result = await FilePicker.platform.pickFiles(
      type: FileType.custom,
      allowedExtensions: const ['png', 'jpg', 'jpeg', 'gif', 'webp'],
      allowMultiple: true,
      withData: true,
    );
    if (result == null) return const [];
    final d = await dir();
    final out = <File>[];
    for (final f in result.files) {
      final bytes = f.bytes;
      if (bytes == null) continue;
      final name = '${DateTime.now().microsecondsSinceEpoch}_${f.name}';
      final file = File('${d.path}/$name');
      await file.writeAsBytes(bytes);
      out.add(file);
    }
    return out;
  }

  static Future<void> remove(File f) async {
    if (await f.exists()) await f.delete();
  }
}

/// 表情面板:四列网格,最后一格是"+ 导入",长按删除。
class StickerPanel extends StatefulWidget {
  const StickerPanel({super.key, required this.onPick, this.height = 232});

  final ValueChanged<File> onPick;
  final double height;

  @override
  State<StickerPanel> createState() => _StickerPanelState();
}

class _StickerPanelState extends State<StickerPanel> {
  List<File> _items = const [];
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _reload();
  }

  Future<void> _reload() async {
    final items = await StickerStore.list();
    if (!mounted) return;
    setState(() {
      _items = items;
      _loading = false;
    });
  }

  Future<void> _import() async {
    final added = await StickerStore.import();
    if (!mounted) return;
    if (added.isNotEmpty) {
      await _reload();
    } else {
      await _reload();
    }
  }

  @override
  Widget build(BuildContext context) {
    final dsh = DshColors.of(context);
    if (_loading) {
      return SizedBox(
        height: widget.height,
        child: const Center(child: SizedBox.square(dimension: 20, child: CircularProgressIndicator(strokeWidth: 2))),
      );
    }
    final cells = <Widget>[
      for (final f in _items)
        _StickerCell(
          file: f,
          onTap: () => widget.onPick(f),
          onLongPress: () async {
            await StickerStore.remove(f);
            await _reload();
          },
        ),
      _ImportCell(onTap: _import),
    ];

    return SizedBox(
      height: widget.height,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(4, 2, 4, 6),
            child: Row(
              children: [
                Text('表情包', style: Theme.of(context).textTheme.labelMedium),
                const Spacer(),
                Text(
                  _items.isEmpty ? '还没有，点右边导入图片或 GIF' : '长按可删除',
                  style: Theme.of(context).textTheme.labelSmall?.copyWith(color: dsh.textTertiary),
                ),
              ],
            ),
          ),
          Expanded(
            child: GridView.count(
              crossAxisCount: 4,
              mainAxisSpacing: 8,
              crossAxisSpacing: 8,
              children: cells,
            ),
          ),
        ],
      ),
    );
  }
}

class _StickerCell extends StatelessWidget {
  const _StickerCell({required this.file, required this.onTap, required this.onLongPress});

  final File file;
  final VoidCallback onTap;
  final VoidCallback onLongPress;

  @override
  Widget build(BuildContext context) {
    return Pressable(
      onTap: onTap,
      onLongPress: onLongPress,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(AppShape.sm),
        child: Image.file(
          file,
          fit: BoxFit.cover,
          gaplessPlayback: true,
          errorBuilder: (_, __, ___) => const SizedBox.shrink(),
        ),
      ),
    );
  }
}

class _ImportCell extends StatelessWidget {
  const _ImportCell({required this.onTap});

  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final dsh = DshColors.of(context);
    return Pressable(
      onTap: onTap,
      child: DecoratedBox(
        decoration: ShapeDecoration(
          color: Theme.of(context).colorScheme.onSurface.withValues(alpha: 0.05),
          shape: AppShape.outlined(
            Theme.of(context).colorScheme.outlineVariant.withValues(alpha: 0.4),
            radius: AppShape.sm,
          ),
        ),
        child: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(AppIcons.plus, size: 18, color: dsh.textTertiary),
              const SizedBox(height: 2),
              Text('导入', style: TextStyle(fontSize: 10, color: dsh.textTertiary)),
            ],
          ),
        ),
      ),
    );
  }
}
