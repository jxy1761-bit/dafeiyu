import 'package:flutter/material.dart';

import '../../ui/common/pressable.dart';
import '../../ui/common/status_dot.dart';
import '../../ui/theme/theme.dart';

/// 左侧抽屉:功能入口的扩展位。
///
/// 和 dsh-Go 的会话抽屉同一个角色 —— 从左边滑出来,一块圆角 28 的玻璃,
/// 里面按「会话 / 连接」分组,每行 44 高、圆角 14、按下有水波。以后加
/// 语音、形象、记忆这些页,都往这里的组里塞一行就行。
class ChatDrawer extends StatelessWidget {
  const ChatDrawer({
    super.key,
    required this.title,
    required this.subtitle,
    required this.tone,
    this.onNewChat,
    required this.onReconnect,
    required this.onSettings,
    required this.onClose,
    this.onClear,
    this.onDelete,
    this.shared = true,
    this.messageCount = 0,
    this.sessionLabel = '共享会话',
    this.liveConnected = false,
  });

  final String title;
  final String subtitle;
  final StatusTone tone;
  final VoidCallback? onNewChat;
  final VoidCallback onReconnect;
  final VoidCallback onSettings;
  final VoidCallback onClose;

  /// 清空聊天记录(保留会话)。
  final VoidCallback? onClear;

  /// 删除整个会话(连附件一起)。
  final VoidCallback? onDelete;

  /// 当前是不是共享会话:两个破坏性操作的文案要按它变。
  final bool shared;
  final int messageCount;

  /// 会话模式说明:共享 / 隔离(本机 id)。
  final String sessionLabel;

  /// Hub 实时订阅是否在线。
  final bool liveConnected;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final dsh = DshColors.of(context);

    return Drawer(
      width: 300,
      backgroundColor: Colors.transparent,
      elevation: 0,
      shape: const RoundedRectangleBorder(),
      child: SafeArea(
        right: false,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(8, 8, 0, 8),
          child: GlassSurface(
            tier: GlassTier.sheet,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                // 头部:头像 + 名字 + 状态
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 18, 16, 14),
                  child: Row(
                    children: [
                      Container(
                        width: 42,
                        height: 42,
                        decoration: BoxDecoration(
                          shape: BoxShape.circle,
                          border: Border.all(
                            color: theme.colorScheme.outlineVariant.withValues(alpha: 0.45),
                          ),
                        ),
                        clipBehavior: Clip.antiAlias,
                        child: Image.asset(
                          'assets/dafeiyu/avatar.png',
                          fit: BoxFit.cover,
                          errorBuilder: (_, __, ___) => const SizedBox.shrink(),
                        ),
                      ),
                      const SizedBox(width: AppSpace.md),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Text(
                              title,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: theme.textTheme.titleSmall?.copyWith(fontSize: 16),
                            ),
                            const SizedBox(height: 2),
                            Row(
                              children: [
                                StatusDot(tone: tone, size: 6),
                                const SizedBox(width: 5),
                                Flexible(
                                  child: Text(
                                    subtitle,
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                    style: theme.textTheme.labelSmall?.copyWith(color: dsh.textTertiary),
                                  ),
                                ),
                              ],
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
                _Rule(),
                _GroupLabel('会话'),
                if (onNewChat != null)
                  _Row(icon: AppIcons.newChat, label: '新对话', onTap: onNewChat!)
                else
                  _Note(icon: AppIcons.link, text: '所有设备共享同一段对话'),
                _Row(
                  icon: AppIcons.menu,
                  label: sessionLabel,
                  trailing: messageCount > 0 ? '$messageCount 条' : null,
                  selected: true,
                  onTap: onClose,
                ),
                if (messageCount > 0 && onClear != null)
                  _Row(
                    icon: AppIcons.delete,
                    label: '清空聊天记录',
                    onTap: onClear!,
                  ),
                if (onDelete != null)
                  _Row(
                    icon: AppIcons.delete,
                    label: shared ? '删除共享会话' : '删除此会话',
                    danger: true,
                    onTap: onDelete!,
                  ),
                const SizedBox(height: AppSpace.sm),
                _Rule(),
                _GroupLabel('连接'),
                _Row(icon: AppIcons.refresh, label: '重新连接', onTap: onReconnect),
                _Note(
                  icon: AppIcons.link,
                  text: liveConnected ? '实时同步已开启' : '实时同步未连接（直连模式）',
                ),
                _Row(icon: AppIcons.settings, label: '连接设置', onTap: onSettings),
                const Spacer(),
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
                  child: Text(
                    '局域网 · AstrBot webchat',
                    style: theme.textTheme.labelSmall?.copyWith(color: dsh.textDisabled),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _Rule extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12),
      child: Divider(
        height: 1,
        thickness: 1,
        color: Theme.of(context).colorScheme.outlineVariant.withValues(alpha: 0.28),
      ),
    );
  }
}

class _GroupLabel extends StatelessWidget {
  const _GroupLabel(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 14, 16, 6),
      child: Text(
        text,
        style: Theme.of(context).textTheme.labelSmall?.copyWith(
              color: DshColors.of(context).textTertiary,
              letterSpacing: 0.4,
            ),
      ),
    );
  }
}

/// 不可点的一行说明:共享会话时顶替「新对话」的位置。
class _Note extends StatelessWidget {
  const _Note({required this.icon, required this.text});

  final IconData icon;
  final String text;

  @override
  Widget build(BuildContext context) {
    final dsh = DshColors.of(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(18, 10, 16, 10),
      child: Row(
        children: [
          Icon(icon, size: 16, color: dsh.textTertiary),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              text,
              style: Theme.of(context).textTheme.labelSmall?.copyWith(
                    color: dsh.textTertiary,
                    height: 1.5,
                  ),
            ),
          ),
        ],
      ),
    );
  }
}

class _Row extends StatelessWidget {
  const _Row({
    required this.icon,
    required this.label,
    required this.onTap,
    this.trailing,
    this.selected = false,
    this.danger = false,
  });

  final IconData icon;
  final String label;
  final VoidCallback onTap;
  final String? trailing;
  final bool selected;
  final bool danger;

  static const _height = 44.0;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final dsh = DshColors.of(context);
    final colors = theme.colorScheme;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 1),
      child: Pressable(
        onTap: onTap,
        child: Container(
          height: _height,
          padding: const EdgeInsets.symmetric(horizontal: 10),
          decoration: ShapeDecoration(
            color: selected ? colors.primary.withValues(alpha: 0.10) : Colors.transparent,
            shape: AppShape.outlined(
              selected ? Colors.transparent : Colors.transparent,
              radius: AppShape.md,
            ),
          ),
          child: Row(
            children: [
              Icon(
                icon,
                size: 20,
                color: danger
                    ? dsh.danger
                    : (selected ? colors.primary : colors.onSurface),
              ),
              const SizedBox(width: AppSpace.md),
              Expanded(
                child: Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.bodyMedium?.copyWith(
                    color: danger ? dsh.danger : (selected ? colors.primary : colors.onSurface),
                  ),
                ),
              ),
              if (trailing != null)
                Text(
                  trailing!,
                  style: theme.textTheme.labelSmall?.copyWith(color: dsh.textTertiary),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
