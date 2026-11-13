import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../astrbot/models.dart';
import '../../ui/common/pressable.dart';
import '../../ui/common/streaming_cursor.dart';
import '../../ui/common/typing_indicator.dart';
import '../../ui/theme/theme.dart';
import '../media/media_bits.dart';

/// 用户消息:右对齐的气泡,最宽 82%,20 圆角的连续曲率,内边距 16×8。
///
/// 一条消息里可能有文字 + 若干媒体(图 / 语音 / 文件),按顺序竖着排;
/// 只有媒体没有文字时不画气泡,只给媒体本身。
class UserBubble extends StatelessWidget {
  const UserBubble({super.key, required this.parts});

  final List<MsgPart> parts;

  Future<void> _copy(BuildContext context, String text) async {
    await Clipboard.setData(ClipboardData(text: text));
    if (!context.mounted) return;
    HapticFeedback.selectionClick();
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(const SnackBar(content: Text('已复制'), duration: Duration(seconds: 1)));
  }

  @override
  Widget build(BuildContext context) {
    final surfaces = DshSurfaces.of(context);
    final style = AppType.chatBody(context, color: surfaces.onUserBubble);

    return LayoutBuilder(
      builder: (context, constraints) {
        final maxWidth = constraints.maxWidth * 0.82;
        final children = <Widget>[];
        for (final p in parts) {
          switch (p) {
            case TextPart(:final text):
              if (text.trim().isEmpty) continue;
              children.add(
                Pressable(
                  onLongPress: () => _copy(context, text),
                  child: DecoratedBox(
                    decoration: ShapeDecoration(color: surfaces.userBubble, shape: AppShape.bubble),
                    child: Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                      child: Text(text, style: style),
                    ),
                  ),
                ),
              );
            case MediaPart(isImage: true):
              children.add(MediaImage(part: p, maxWidth: maxWidth * 0.9, maxHeight: 300));
            case MediaPart(isVoice: true):
              children.add(VoiceBubble(part: p, mine: true));
            case MediaPart():
              children.add(FileCard(part: p, mine: true));
            default:
              break;
          }
        }
        if (children.isEmpty) return const SizedBox.shrink();

        return Column(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            for (var i = 0; i < children.length; i++)
              Padding(
                padding: EdgeInsets.only(top: i == 0 ? 0 : 6),
                child: ConstrainedBox(
                  constraints: BoxConstraints(maxWidth: maxWidth),
                  child: children[i],
                ),
              ),
          ],
        );
      },
    );
  }
}

/// 她的回复:**没有气泡、没有头像**的全宽裸排(Claude / dsh-Go 的做法)。
///
/// 媒体也走这条裸排:图比文字宽一点,语音和文件是胶囊,都在左边。
class AssistantMessage extends StatelessWidget {
  const AssistantMessage({
    super.key,
    required this.parts,
    this.streaming = false,
    this.showActions = false,
  });

  final List<MsgPart> parts;
  final bool streaming;
  final bool showActions;

  @override
  Widget build(BuildContext context) {
    final style = AppType.chatBody(context);
    final text = parts.whereType<TextPart>().map((p) => p.text).join();

    final blocks = <Widget>[];
    for (final p in parts) {
      switch (p) {
        case TextPart(:final text):
          if (text.trim().isEmpty) continue;
          blocks.add(
            Text.rich(
              TextSpan(
                children: [
                  TextSpan(text: text),
                  if (streaming) const WidgetSpan(child: StreamingCursor()),
                ],
              ),
              style: style,
            ),
          );
        case MediaPart(isImage: true):
          blocks.add(MediaImage(part: p, maxWidth: 260, maxHeight: 320));
        case MediaPart(isVoice: true):
          blocks.add(Align(alignment: Alignment.centerLeft, child: VoiceBubble(part: p)));
        case MediaPart():
          blocks.add(Align(alignment: Alignment.centerLeft, child: FileCard(part: p)));
        default:
          break;
      }
    }

    if (blocks.isEmpty && streaming) {
      return const Padding(padding: EdgeInsets.symmetric(vertical: 6), child: TypingIndicator());
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (var i = 0; i < blocks.length; i++)
          Padding(padding: EdgeInsets.only(top: i == 0 ? 0 : 8), child: blocks[i]),
        if (showActions && text.isNotEmpty) ...[
          const SizedBox(height: 2),
          _Actions(text: text),
        ],
      ],
    );
  }
}

class _Actions extends StatefulWidget {
  const _Actions({required this.text});
  final String text;

  @override
  State<_Actions> createState() => _ActionsState();
}

class _ActionsState extends State<_Actions> {
  bool _done = false;

  Future<void> _copy() async {
    await Clipboard.setData(ClipboardData(text: widget.text));
    if (!mounted) return;
    HapticFeedback.selectionClick();
    setState(() => _done = true);
    await Future<void>.delayed(const Duration(milliseconds: 1200));
    if (mounted) setState(() => _done = false);
  }

  @override
  Widget build(BuildContext context) {
    final dsh = DshColors.of(context);
    return Row(
      children: [
        Pressable(
          onTap: _copy,
          semanticLabel: '复制',
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
            child: Icon(
              _done ? AppIcons.done : AppIcons.copy,
              size: 16,
              color: _done ? dsh.success : dsh.textTertiary,
            ),
          ),
        ),
      ],
    );
  }
}

/// 一条提示(出错 / 系统消息):居中一行小字,不打断气泡的节奏。
class NoticeRow extends StatelessWidget {
  const NoticeRow({super.key, required this.text, this.warn = false});

  final String text;
  final bool warn;

  @override
  Widget build(BuildContext context) {
    final dsh = DshColors.of(context);
    final style = Theme.of(context).textTheme.labelSmall?.copyWith(
      color: warn ? dsh.warning : dsh.textTertiary,
      height: 1.4,
    );
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        if (warn) ...[
          Icon(AppIcons.warning, size: 13, color: dsh.warning),
          const SizedBox(width: 5),
        ],
        Flexible(child: Text(text, textAlign: TextAlign.center, style: style)),
      ],
    );
  }
}
