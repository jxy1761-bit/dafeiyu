import 'package:flutter/material.dart';

import '../../ui/common/fade_in.dart';
import '../../ui/common/pressable.dart';
import '../../ui/theme/theme.dart';

/// 新对话页。
///
/// 照 dsh-Go 的空状态:垂直略偏上居中,一个字标、一行说明,下面是几条
/// 建议——点了把话填进输入条,不直接发,让人还能改。
class EmptyState extends StatelessWidget {
  const EmptyState({
    super.key,
    required this.topInset,
    required this.onSuggestion,
    this.connected = false,
    this.title = '大肥鱼',
    this.subtitle,
  });

  final double topInset;
  final ValueChanged<String> onSuggestion;
  final bool connected;
  final String title;
  final String? subtitle;

  static const _suggestions = <(String, String)>[
    ('今天过得怎么样？', '随便聊聊'),
    ('陪我待一会儿吧', '什么都不用做'),
    ('我有点累', '说说心事'),
  ];

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final dsh = DshColors.of(context);

    return Padding(
      padding: EdgeInsets.only(top: topInset),
      child: Align(
        alignment: const Alignment(0, -0.28),
        child: SingleChildScrollView(
          padding: const EdgeInsets.symmetric(horizontal: AppSpace.xxl),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              FadeSlideIn(
                child: Container(
                  width: 76,
                  height: 76,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    border: Border.all(color: Theme.of(context).colorScheme.outlineVariant.withValues(alpha: 0.45), width: 1),
                  ),
                  clipBehavior: Clip.antiAlias,
                  child: Image.asset(
                    'assets/dafeiyu/avatar.png',
                    fit: BoxFit.cover,
                    errorBuilder: (_, __, ___) => const SizedBox.shrink(),
                  ),
                ),
              ),
              const SizedBox(height: AppSpace.lg),
              FadeSlideIn(
                delay: const Duration(milliseconds: 60),
                child: Text(title, style: AppType.display(size: 26)),
              ),
              const SizedBox(height: 6),
              FadeSlideIn(
                delay: const Duration(milliseconds: 110),
                child: Text(
                  subtitle ?? (connected ? '在的，说吧。' : '还没连上服务器'),
                  textAlign: TextAlign.center,
                  style: theme.textTheme.bodyMedium?.copyWith(color: dsh.textTertiary, height: 1.5),
                ),
              ),
              const SizedBox(height: AppSpace.xxl),
              for (var i = 0; i < _suggestions.length; i++)
                Padding(
                  padding: const EdgeInsets.only(bottom: AppSpace.sm),
                  child: FadeSlideIn(
                    delay: Duration(milliseconds: 150 + i * 45),
                    child: _Suggestion(
                      label: _suggestions[i].$1,
                      hint: _suggestions[i].$2,
                      onTap: () => onSuggestion(_suggestions[i].$1),
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class _Suggestion extends StatelessWidget {
  const _Suggestion({required this.label, required this.hint, required this.onTap});

  final String label;
  final String hint;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final dsh = DshColors.of(context);
    return Pressable(
      onTap: onTap,
      child: GlassSurface(
        tier: GlassTier.card,
        borderRadius: AppShape.radiusLg,
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(label, style: theme.textTheme.bodyMedium),
            const SizedBox(width: AppSpace.sm),
            Text(hint, style: theme.textTheme.labelSmall?.copyWith(color: dsh.textTertiary)),
          ],
        ),
      ),
    );
  }
}
