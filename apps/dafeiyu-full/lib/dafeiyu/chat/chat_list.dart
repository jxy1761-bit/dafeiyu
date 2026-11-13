import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../ui/common/pressable.dart';
import '../../ui/common/stream_motion.dart';
import '../../astrbot/astrbot_client.dart';
import '../../ui/theme/theme.dart';
import 'chat_controller.dart';
import 'rows.dart';

/// 消息列表。
///
/// 沿用 dsh-Go 的**居中锚定**滚动:视口切成「中心之前 / 中心之后」两段,
/// 整段历史放在中心之前,于是
///
/// ```
///   ┌ 历史(往上长)┐
///   └──── center ────┘  ← 零偏移正好落在输入条上沿
///   ┌ 底部留白 ┐
/// ```
///
/// - 新消息接在中心之前的最下端:底边不动,新的一行从下面长出来,
///   正在看上面的人不会被推走;
/// - 短会话自然贴底,不需要开屏先跳一次;
/// - `anchor` 取「输入条上沿在视口里的比例」,零偏移就是贴底。
class ChatList extends StatefulWidget {
  const ChatList({
    super.key,
    required this.chat,
    required this.topInset,
    required this.bottomInset,
    this.scrolledUnder,
    this.onRetry,
  });

  final ChatController chat;

  /// 顶栏占的高度(含状态栏)。
  final double topInset;

  /// 视口向输入条下面伸出去多少(常量,布局在页面那一层就排好了)。
  final double bottomInset;

  /// 内容从顶栏下面滚过去的程度(0..1),顶栏靠它淡入底色。
  final ValueNotifier<double>? scrolledUnder;

  final VoidCallback? onRetry;

  @override
  State<ChatList> createState() => _ChatListState();
}

class _ChatListState extends State<ChatList> {
  final _scroll = ScrollController();
  final _centerKey = GlobalKey();
  final _showJump = ValueNotifier<bool>(false);

  /// 看见过的行:只有第一次出现才播入场(滚回去不重播)。
  final _seen = <String>{};

  @override
  void initState() {
    super.initState();
    _scroll.addListener(_onScroll);
  }

  @override
  void dispose() {
    _scroll
      ..removeListener(_onScroll)
      ..dispose();
    _showJump.dispose();
    super.dispose();
  }

  void _onScroll() {
    if (!_scroll.hasClients) return;
    // 居中锚定下,往上翻是负偏移。
    final off = _scroll.offset;
    widget.scrolledUnder?.value = (-off / 72).clamp(0.0, 1.0);
    final jump = off < -90;
    if (_showJump.value != jump) _showJump.value = jump;
  }

  void jumpToBottom() {
    if (!_scroll.hasClients) return;
    _scroll.animateTo(
      0,
      duration: AppMotion.of(context, AppMotion.standard),
      curve: AppMotion.std,
    );
  }

  @override
  Widget build(BuildContext context) {
    final items = widget.chat.items;
    if (items.isEmpty) {
      return _EmptyArea(topInset: widget.topInset, onRetry: widget.onRetry);
    }

    // SliverList 的顺序是「靠中心的在前」:第 0 个子项就是最新一条。
    final order = <int>[for (var i = items.length - 1; i >= 0; i--) i];
    final indexOf = <String, int>{
      for (var k = 0; k < order.length; k++) items[order[k]].id: k,
    };

    return LayoutBuilder(
      builder: (context, constraints) {
        final viewport = constraints.maxHeight;
        final bottomPad = widget.bottomInset + AppSpace.sm;
        final anchor = viewport <= 0 ? 1.0 : ((viewport - bottomPad) / viewport).clamp(0.0, 1.0);

        return Stack(
          children: [
            NotificationListener<ScrollNotification>(
              onNotification: (_) => false,
              child: CustomScrollView(
                controller: _scroll,
                center: _centerKey,
                anchor: anchor,
                physics: const ClampingScrollPhysics(),
                scrollBehavior: ScrollConfiguration.of(context).copyWith(overscroll: false),
                keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
                slivers: [
                  SliverToBoxAdapter(
                    child: SizedBox(height: widget.topInset + AppSpace.sm),
                  ),
                  // 中心之前:历史。子项 0 贴着中心线(最下面),往上才是更早的。
                  SliverList(
                    delegate: SliverChildBuilderDelegate(
                      (context, i) => _rowFrame(order[i], i == 0),
                      childCount: order.length,
                      findChildIndexCallback: (key) {
                        if (key is! ValueKey<String>) return null;
                        return indexOf[key.value];
                      },
                    ),
                  ),
                  // 中心:零高度的分界。零偏移正好落在 anchor 那条线上。
                  SliverToBoxAdapter(key: _centerKey, child: const SizedBox(height: 0)),
                  // 中心之后:底部留白,让最后一行离输入条有 8。
                  SliverToBoxAdapter(child: SizedBox(height: bottomPad)),
                ],
              ),
            ),
            Positioned(
              right: AppSpace.floatInset,
              bottom: widget.bottomInset + 12,
              child: ValueListenableBuilder<bool>(
                valueListenable: _showJump,
                builder: (context, visible, child) => AnimatedOpacity(
                  opacity: visible ? 1 : 0,
                  duration: AppMotion.of(context, AppMotion.quick),
                  child: AnimatedScale(
                    scale: visible ? 1 : 0.6,
                    duration: AppMotion.of(context, AppMotion.quick),
                    curve: AppMotion.enter,
                    child: child,
                  ),
                ),
                child: _JumpToBottom(onTap: jumpToBottom),
              ),
            ),
          ],
        );
      },
    );
  }

  Widget _rowFrame(int index, bool newest) {
    final item = widget.chat.items[index];
    final prev = index > 0 ? widget.chat.items[index - 1] : null;
    // 同一人、三分钟内算一轮,间距 8;换轮 24。
    final sameTurn = prev != null &&
        prev.role == item.role &&
        item.time.difference(prev.time).inMinutes < 3;
    final gap = index == 0
        ? AppSpace.md
        : (sameTurn ? AppSpace.sm : AppSpace.betweenTurns);

    final fresh = _seen.add(item.id);
    final body = StreamFadeIn(
      enabled: fresh,
      rise: item.mine,
      child: switch (item.role) {
        ChatRole.me => UserBubble(parts: item.parts),
        ChatRole.her => AssistantMessage(
            parts: item.parts,
            streaming: item.streaming,
            showActions: newest && !item.streaming && item.text.trim().isNotEmpty,
          ),
      },
    );

    return KeyedSubtree(
      key: ValueKey<String>(item.id),
      child: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: AppSpace.readingWidth),
          child: Padding(
            padding: EdgeInsets.fromLTRB(AppSpace.gutter, gap, AppSpace.gutter, 0),
            // 撑满阅读宽度,否则短行会被 Center 居中
            child: SizedBox(width: double.infinity, child: body),
          ),
        ),
      ),
    );
  }
}

/// 空会话:居中一句提示 + 一枚重试键。真正的新对话页在 [EmptyState]。
class _EmptyArea extends StatelessWidget {
  const _EmptyArea({required this.topInset, this.onRetry});

  final double topInset;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) {
    final dsh = DshColors.of(context);
    return Padding(
      padding: EdgeInsets.only(top: topInset),
      child: Align(
        alignment: const Alignment(0, -0.2),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              '还没有聊天',
              style: Theme.of(context).textTheme.titleSmall?.copyWith(color: dsh.textTertiary),
            ),
            if (onRetry != null) ...[
              const SizedBox(height: AppSpace.md),
              Pressable(
                onTap: onRetry,
                child: GlassSurface(
                  tier: GlassTier.pill,
                  stadium: true,
                  padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                  child: Text('重新连接', style: Theme.of(context).textTheme.labelLarge),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _JumpToBottom extends StatelessWidget {
  const _JumpToBottom({required this.onTap});

  final VoidCallback onTap;

  static const _size = 44.0;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    return SizedBox.square(
      dimension: _size,
      child: GlassSurface(
        tier: GlassTier.chrome,
        // 悬浮在滚动内容上,每帧重采样背景太贵,这颗不模糊。
        blur: false,
        circle: true,
        onTap: onTap,
        child: Center(child: Icon(AppIcons.scrollToBottom, size: 22, color: colors.onSurface)),
      ),
    );
  }
}

/// 给外面算阅读列宽度用(和 dsh-Go 的 readingWidth 同一档)。
double readingInset(double width) => math.max(0, (width - AppSpace.readingWidth) / 2);
