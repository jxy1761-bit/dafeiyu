import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../ui/theme/theme.dart';

const double _keySize = 36;
const double _keyGap = 6;
const double _fontSize = 15.0;
const double _lineHeight = 1.45;

/// 输入条。
///
/// 结构和 dsh-Go 一致:一枚 28 圆角的玻璃胶囊,里面一行 —— 右端一颗 36 的
/// 圆键(能发就是主色,不能发是 12% 的底色),左边留 18(没有附件键时)。
/// 多行时胶囊往上长,行高用 [SmoothSize] 平滑过渡,不跳。
class Composer extends StatefulWidget {
  const Composer({
    super.key,
    required this.controller,
    required this.focusNode,
    required this.onSend,
    required this.onStop,
    required this.running,
    required this.connected,
    this.hint,
    this.maxLines = 6,
    this.leading,
    this.tray,
    this.hasDrafts = false,
  });

  final TextEditingController controller;
  final FocusNode focusNode;
  final VoidCallback onSend;
  final VoidCallback onStop;
  final bool running;
  final bool connected;
  final String? hint;
  final int maxLines;

  /// 左边那几颗键(+)附件、表情)。
  final Widget? leading;

  /// 文字上方的一条(待发附件的缩略图)。
  final Widget? tray;

  /// 有没有待发附件:只有图没有字时发送键也该亮。
  final bool hasDrafts;

  @override
  State<Composer> createState() => _ComposerState();
}

class _ComposerState extends State<Composer> {
  bool _pressed = false;

  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_onText);
  }

  @override
  void didUpdateWidget(covariant Composer old) {
    super.didUpdateWidget(old);
    if (old.controller != widget.controller) {
      old.controller.removeListener(_onText);
      widget.controller.addListener(_onText);
    }
  }

  @override
  void dispose() {
    widget.controller.removeListener(_onText);
    super.dispose();
  }

  // 只是为了让发送键在「空 / 非空」之间换颜色
  void _onText() {
    if (mounted) setState(() {});
  }

  void _send() {
    if (widget.running) {
      HapticFeedback.mediumImpact();
      widget.onStop();
      return;
    }
    if (widget.controller.text.trim().isEmpty || !widget.connected) return;
    HapticFeedback.lightImpact();
    widget.onSend();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final dsh = DshColors.of(context);
    final line = MediaQuery.textScalerOf(context).scale(_fontSize) * _lineHeight;
    // 一行文字在 36 的键里居中,不靠猜。
    final textPad = math.max(4.0, (_keySize - line) / 2);
    final empty = widget.controller.text.trim().isEmpty;
    final canSend = widget.connected && (!empty || widget.hasDrafts);
    final showStop = widget.running && empty;

    return GlassSurface(
      tier: GlassTier.chrome,
      borderRadius: AppShape.radiusXl,
      child: Padding(
        padding: EdgeInsets.fromLTRB(widget.leading == null ? 18 : _keyGap, _keyGap, _keyGap, _keyGap),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (widget.tray != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(2, 2, 2, _keyGap),
                child: widget.tray!,
              ),
            Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            if (widget.leading != null) ...[
              widget.leading!,
              const SizedBox(width: _keyGap),
            ],
            Expanded(
              child: TextField(
                controller: widget.controller,
                focusNode: widget.focusNode,
                minLines: 1,
                maxLines: widget.maxLines,
                keyboardType: TextInputType.multiline,
                textInputAction: TextInputAction.newline,
                textCapitalization: TextCapitalization.sentences,
                style: theme.textTheme.bodyLarge?.copyWith(
                  fontSize: _fontSize,
                  height: _lineHeight,
                ),
                cursorColor: theme.colorScheme.primary,
                decoration: InputDecoration(
                  hintText: widget.hint ?? '说点什么…',
                  hintMaxLines: 1,
                  hintStyle: theme.textTheme.bodyLarge?.copyWith(
                    fontSize: _fontSize,
                    color: dsh.textTertiary,
                    height: _lineHeight,
                  ),
                  filled: false,
                  isDense: true,
                  border: InputBorder.none,
                  enabledBorder: InputBorder.none,
                  focusedBorder: InputBorder.none,
                  disabledBorder: InputBorder.none,
                  contentPadding: EdgeInsets.symmetric(vertical: textPad),
                ),
              ),
            ),
            const SizedBox(width: _keyGap),
            _SendKey(
              mode: showStop ? _KeyMode.stop : _KeyMode.send,
              enabled: showStop || canSend,
              busy: widget.running && !showStop,
              pressed: _pressed,
              onTapDown: () => setState(() => _pressed = true),
              onTapUp: () => setState(() => _pressed = false),
              onTap: _send,
            ),
          ],
        ),
          ],
        ),
      ),
    );
  }
}

enum _KeyMode { send, stop }

class _SendKey extends StatelessWidget {
  const _SendKey({
    required this.mode,
    required this.enabled,
    required this.busy,
    required this.pressed,
    required this.onTapDown,
    required this.onTapUp,
    required this.onTap,
  });

  final _KeyMode mode;
  final bool enabled;
  final bool busy;
  final bool pressed;
  final VoidCallback onTapDown;
  final VoidCallback onTapUp;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    final active = enabled || busy;
    final bg = active ? colors.primary : colors.onSurface.withValues(alpha: 0.12);
    final fg = active ? colors.onPrimary : colors.onSurface.withValues(alpha: 0.38);
    final (icon, size) = mode == _KeyMode.send
        ? (AppIcons.send, 20.0)
        : (AppIcons.stopGenerating, 18.0);

    return Semantics(
      button: true,
      enabled: enabled,
      label: mode == _KeyMode.send ? '发送' : '停止',
      child: GestureDetector(
        onTap: enabled ? onTap : null,
        onTapDown: enabled ? (_) => onTapDown() : null,
        onTapUp: enabled ? (_) => onTapUp() : null,
        onTapCancel: onTapUp,
        child: AnimatedScale(
          scale: pressed ? 0.9 : 1,
          duration: pressed ? AppMotion.press : AppMotion.quick,
          curve: AppMotion.std,
          child: AnimatedContainer(
            duration: AppMotion.quick,
            width: _keySize,
            height: _keySize,
            decoration: BoxDecoration(color: bg, shape: BoxShape.circle),
            child: AnimatedSwitcher(
              duration: AppMotion.quick,
              switchInCurve: AppMotion.enter,
              switchOutCurve: AppMotion.exit,
              child: busy
                  ? Padding(
                      key: const ValueKey('busy'),
                      padding: const EdgeInsets.all(10),
                      child: CircularProgressIndicator(strokeWidth: 2, color: fg),
                    )
                  : Icon(icon, key: ValueKey(mode), size: size, color: fg),
            ),
          ),
        ),
      ),
    );
  }
}
