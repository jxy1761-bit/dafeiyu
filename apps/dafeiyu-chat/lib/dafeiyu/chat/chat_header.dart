import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../ui/common/status_dot.dart';
import '../../ui/theme/theme.dart';
import 'chat_controller.dart';

/// 聊天页顶栏。
///
/// 结构照搬 dsh-Go:一条 56 高的浮条,三个部分各是一块玻璃——
///
/// ```
/// (≡)  ┌ 标题 / ● 状态 ┐            (✎)
///  44     胶囊(min 44)               44
/// ```
///
/// 内容滚到下面去之后,顶上才淡入一层背景(`TopFade`),静止时整页是一块
/// 完整的背景,没有横条、没有分隔线。
class ChatHeader extends StatelessWidget {
  const ChatHeader({
    super.key,
    required this.title,
    required this.status,
    required this.tone,
    this.scrolledUnder,
    this.onOpenMenu,
    this.onNewChat,
    this.onTitleTap,
    this.detail,
  });

  final String title;
  final String status;
  final StatusTone tone;

  /// 内容从顶栏下面滚过去的程度(0..1);null = 不淡入。
  final ValueNotifier<double>? scrolledUnder;

  final VoidCallback? onOpenMenu;
  final VoidCallback? onNewChat;
  final VoidCallback? onTitleTap;

  /// 状态行后面接的一段(「· webchat」之类),没有就不占位。
  final String? detail;

  static const barHeight = 56.0;

  static double pillHeightOf(BuildContext context) {
    final scaler = MediaQuery.textScalerOf(context);
    return math.max(44.0, scaler.scale(15) * 1.3 + scaler.scale(11) * 1.25 + 8);
  }

  static double barHeightOf(BuildContext context) => math.max(barHeight, pillHeightOf(context) + 12);

  /// 页面顶端要给它让出的高度。
  static double heightOf(BuildContext context) => MediaQuery.paddingOf(context).top + barHeightOf(context) + 8;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;
    final dsh = DshColors.of(context);
    final top = MediaQuery.paddingOf(context).top;
    final bar = barHeightOf(context);
    final pill = pillHeightOf(context);
    final titleStyle = theme.textTheme.titleSmall?.copyWith(fontSize: 15, height: 1.3);
    final statusStyle = theme.textTheme.labelSmall?.copyWith(fontSize: 11, height: 1.25);

    // 淡层要盖过顶栏再多 12,否则会在胶囊下面切出一条硬线。
    return Stack(
      clipBehavior: Clip.none,
      children: [
        Positioned(
          left: 0,
          right: 0,
          top: 0,
          child: scrolledUnder == null
              ? const SizedBox.shrink()
              : ValueListenableBuilder<double>(
                  valueListenable: scrolledUnder!,
                  builder: (context, t, child) => Opacity(opacity: t, child: child),
                  child: TopFade(height: top + bar + 12),
                ),
        ),
        Padding(
          padding: EdgeInsets.fromLTRB(12, top, 12, 0),
          child: SizedBox(
            height: bar,
            child: Row(
              children: [
                // 左:≡ 打开会话抽屉
                SizedBox.square(
                  dimension: 44,
                  child: GlassSurface(
                    tier: GlassTier.chrome,
                    circle: true,
                    grouped: true,
                    onTap: onOpenMenu,
                    child: Tooltip(
                      message: '会话',
                      child: Center(child: Icon(AppIcons.menu, size: 22, color: colors.onSurface)),
                    ),
                  ),
                ),
                const SizedBox(width: AppSpace.sm),
                // 中:标题 + 状态,一枚胶囊,宽度吃掉剩下的
                Expanded(
                  child: Align(
                    alignment: Alignment.centerLeft,
                    child: GlassSurface(
                      tier: GlassTier.chrome,
                      stadium: true,
                      grouped: true,
                      onTap: onTitleTap,
                      child: ConstrainedBox(
                        constraints: BoxConstraints(minHeight: pill),
                        child: Padding(
                          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
                          child: Column(
                            mainAxisSize: MainAxisSize.min,
                            mainAxisAlignment: MainAxisAlignment.center,
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                title,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: titleStyle,
                              ),
                              Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  StatusDot(tone: tone, size: 6),
                                  const SizedBox(width: 5),
                                  Flexible(
                                    child: Text(
                                      detail == null ? status : '$status · $detail',
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: statusStyle?.copyWith(
                                        color: tone == StatusTone.warn ? dsh.warning : dsh.textTertiary,
                                      ),
                                    ),
                                  ),
                                ],
                              ),
                            ],
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
                // 右:新对话。共享会话下没有「新对话」这回事(所有设备同一个
                // 上下文),这时不画这颗键,而不是画一颗点了没反应的。
                if (onNewChat != null) ...[
                  const SizedBox(width: AppSpace.sm),
                  SizedBox.square(
                    dimension: 44,
                    child: GlassSurface(
                      tier: GlassTier.chrome,
                      circle: true,
                      grouped: true,
                      onTap: onNewChat,
                      child: Tooltip(
                        message: '新对话',
                        child: Center(child: Icon(AppIcons.newChat, size: 21, color: colors.onSurface)),
                      ),
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
      ],
    );
  }
}

