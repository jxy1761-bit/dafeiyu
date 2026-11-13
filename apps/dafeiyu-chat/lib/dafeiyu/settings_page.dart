import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../astrbot/astrbot_client.dart';
import '../astrbot/discovery.dart';
import '../ui/common/pressable.dart';
import '../ui/theme/theme.dart';
import '../full/full_bridge.dart';
import 'power_save.dart';
import 'prefs.dart';

/// 连接设置页。
///
/// 两块:连接(地址可自动识别)与会话(默认共享,可选隔离)。
class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key});

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> with WidgetsBindingObserver {
  late final TextEditingController _url = TextEditingController(text: Prefs.i.baseUrl);
  late final TextEditingController _user = TextEditingController(text: Prefs.i.username);
  late final TextEditingController _pwd = TextEditingController(text: Prefs.i.password);
  late final TextEditingController _session = TextEditingController(text: Prefs.i.deviceSessionId);

  late bool _isolate = Prefs.i.isolateSession;
  late bool _auto = Prefs.i.autoDiscover;
  late bool _powerSaver = Prefs.i.powerSaver;
  late bool _fps120 = Prefs.i.frameRate >= 120;

  String _hint = '';
  bool _ok = false;
  bool _busy = false;

  Map<String, dynamic>? _full;
  bool _fullBusy = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _loadFull();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _loadFull();
  }

  Future<void> _loadFull() async {
    final status = await FullBridge.localStatus();
    if (!mounted || status == null) return;
    setState(() {
      _full = status;
      if (_url.text.trim().isEmpty) {
        _url.text = 'http://127.0.0.1:${status['port'] ?? 6185}';
      }
      if (_user.text.trim().isEmpty) _user.text = 'astrbot';
    });
  }

  Future<void> _fullAction(Future<bool> Function() action, String message) async {
    setState(() {
      _fullBusy = true;
      _hint = message;
    });
    final ok = await action();
    await Future<void>.delayed(const Duration(milliseconds: 900));
    await _loadFull();
    if (!mounted) return;
    setState(() {
      _fullBusy = false;
      _ok = ok;
      _hint = ok ? '$message（已提交，状态稍后刷新）' : '$message失败';
    });
  }

  Future<void> _toggleLan(bool enabled) async {
    setState(() => _fullBusy = true);
    final ok = await FullBridge.setLan(enabled);
    await Future<void>.delayed(const Duration(seconds: 1));
    await _loadFull();
    if (!mounted) return;
    setState(() {
      _fullBusy = false;
      _ok = ok;
      _hint = ok ? (enabled ? '已开放局域网（AstrBot + Hub）' : '已限制为本机访问') : '切换失败';
    });
  }

  void _fillLocal() {
    final port = _full?['port'] ?? 6185;
    setState(() {
      _url.text = 'http://127.0.0.1:$port';
      if (_user.text.trim().isEmpty) _user.text = 'astrbot';
      _hint = '已填入本机地址，密码请手动输入';
      _ok = true;
    });
  }

  String get _fullStatus {
    final full = _full;
    if (full == null) return '';
    if (full['running'] == true) return '运行中';
    if (full['installed'] != true) return '未初始化';
    if (full['desired'] == true) return '启动中';
    return '已停止';
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _url.dispose();
    _user.dispose();
    _pwd.dispose();
    _session.dispose();
    super.dispose();
  }

  bool _commit() {
    final url = _url.text.trim().replaceAll(RegExp(r'/+$'), '');
    if (url.isNotEmpty && !url.startsWith('http://') && !url.startsWith('https://')) {
      setState(() {
        _ok = false;
        _hint = '地址要以 http:// 开头（留空则自动识别）';
      });
      return false;
    }
    if (_user.text.trim().isEmpty || _pwd.text.isEmpty) {
      setState(() {
        _ok = false;
        _hint = '账号和密码都要填';
      });
      return false;
    }
    Prefs.i
      ..baseUrl = url
      ..username = _user.text.trim()
      ..password = _pwd.text
      ..isolateSession = _isolate
      ..autoDiscover = _auto;
    if (_isolate && _session.text.trim().isNotEmpty) {
      Prefs.i.deviceSessionId = _session.text.trim();
    }
    return true;
  }

  Future<void> _test() async {
    if (!_commit()) return;
    setState(() {
      _busy = true;
      _ok = false;
      _hint = '正在连接…';
    });
    try {
      var base = Prefs.i.baseUrl;
      if (base.isEmpty) {
        final hits = await Discovery.scan();
        if (hits.isEmpty) throw '局域网里没找到服务器';
        base = hits.first;
      }
      final c = AstrBotClient(
        baseUrl: base,
        username: Prefs.i.username,
        password: Prefs.i.password,
      );
      await c.ping();
      c.close();
      if (mounted) {
        setState(() {
          _url.text = base;
          _ok = true;
          _hint = '连接成功 · $base';
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _ok = false;
          _hint = '失败：$e';
        });
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// 扫当前局域网,把找到的地址填进去。
  Future<void> _discover() async {
    setState(() {
      _busy = true;
      _ok = false;
      _hint = '正在扫描局域网…';
    });
    try {
      final hits = await Discovery.scan();
      if (!mounted) return;
      setState(() {
        if (hits.isEmpty) {
          _ok = false;
          _hint = '没找到。确认手机和 AstrBot 在同一个 Wi-Fi（接流量时扫不到）';
        } else {
          _url.text = hits.first;
          _ok = true;
          _hint = hits.length == 1
              ? '找到 ${hits.first}'
              : '找到 ${hits.length} 台，已填第一台：${hits.join('  ')}';
        }
      });
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final top = MediaQuery.paddingOf(context).top;
    final dsh = DshColors.of(context);
    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: dshSystemOverlay(Theme.of(context).brightness),
      child: AppBackdrop(
        child: Scaffold(
          backgroundColor: Colors.transparent,
          resizeToAvoidBottomInset: true,
          body: Stack(
            children: [
              ListView(
                padding: EdgeInsets.fromLTRB(AppSpace.gutter, top + 56 + 12, AppSpace.gutter, 40),
                children: [
                  if (_full != null) ...[
                    _GroupLabel('本机服务'),
                    _Section(
                      children: [
                        _Action(
                          icon: AppIcons.settings,
                          label: 'AstrBot · $_fullStatus'
                              '${_full!['hubRunning'] == true ? ' · Hub 已开' : ''}',
                          onTap: _fullBusy ? null : _loadFull,
                        ),
                        if (_full!['installed'] != true) ...[
                          _Divider(),
                          _Action(
                            icon: AppIcons.play,
                            label: _fullBusy ? '处理中…' : '初始化运行环境',
                            onTap: _fullBusy ? null : () => _fullAction(FullBridge.initialize, '初始化运行环境'),
                          ),
                        ],
                        _Divider(),
                        _Action(
                          icon: AppIcons.play,
                          label: _fullBusy ? '处理中…' : '启动 AstrBot',
                          onTap: _fullBusy ? null : () => _fullAction(FullBridge.startLocal, '启动 AstrBot'),
                        ),
                        _Divider(),
                        _Action(
                          icon: AppIcons.stop,
                          label: _fullBusy ? '处理中…' : '停止 AstrBot',
                          onTap: _fullBusy ? null : () => _fullAction(FullBridge.stopLocal, '停止 AstrBot'),
                        ),
                        _Divider(),
                        _Action(
                          icon: AppIcons.link,
                          label: '填入本机 AstrBot 地址',
                          onTap: _fillLocal,
                        ),
                        _Divider(),
                        _Action(
                          icon: AppIcons.settings,
                          label: '打开服务管理（备份 / 日志 / 后台）',
                          onTap: () => FullBridge.openManager(),
                        ),
                        _Divider(),
                        _Switch(
                          label: '允许基础版连接',
                          detail: '把 AstrBot 和 Hub 开放到局域网；基础版用同一 Wi-Fi 连本机。',
                          value: _full!['lan'] == true,
                          onChanged: _fullBusy ? (_) {} : _toggleLan,
                        ),
                      ],
                    ),
                    const SizedBox(height: AppSpace.xl),
                  ],
                  _GroupLabel('连接'),
                  _Section(
                    children: [
                      _Field(label: '服务器', controller: _url, hint: '留空 = 自动识别'),
                      _Divider(),
                      _Action(
                        icon: AppIcons.search,
                        label: _busy ? '正在处理…' : '自动识别局域网里的服务器',
                        onTap: _busy ? null : _discover,
                      ),
                      _Divider(),
                      _Field(label: '用户名', controller: _user, hint: 'astrbot'),
                      _Divider(),
                      _Field(label: '密码', controller: _pwd, hint: '••••••••', obscure: true),
                      _Divider(),
                      _Switch(
                        label: '换网后自动重找',
                        detail: '地址变了(换 WiFi / 热点)就重新扫描,不用手填',
                        value: _auto,
                        onChanged: (v) => setState(() => _auto = v),
                      ),
                    ],
                  ),
                  const SizedBox(height: AppSpace.xl),
                  _GroupLabel('会话'),
                  _Section(
                    children: [
                      _Switch(
                        label: '按设备隔离会话',
                        detail: '关（默认）：所有设备共用 shared 会话，谁说什么彼此都看得见；'
                            '开：本机用一个独立会话，别的设备看不到这边的对话。',
                        value: _isolate,
                        onChanged: (v) => setState(() => _isolate = v),
                      ),
                      if (_isolate) ...[
                        _Divider(),
                        _Field(label: '会话标识', controller: _session, hint: 'device-1234'),
                      ],
                    ],
                  ),
                  const SizedBox(height: AppSpace.xl),
                  _GroupLabel('性能'),
                  _Section(
                    children: [
                      _Switch(
                        label: '省电模式',
                        detail: '暂停聊天里的动态表情/动图，只显示首帧，明显降低持续渲染、发热与耗电。',
                        value: _powerSaver,
                        onChanged: (v) {
                          setState(() => _powerSaver = v);
                          PowerSave.set(v);
                        },
                      ),
                      _Divider(),
                      _Switch(
                        label: '120 Hz 高刷',
                        detail: '屏幕支持时请求 120Hz；若系统仍为 60，请到系统显示设置把刷新率设为 120。',
                        value: _fps120,
                        onChanged: (v) {
                          setState(() => _fps120 = v);
                          Prefs.i.frameRate = v ? 120 : 60;
                          FullBridge.setFrameRate(v ? 120 : 60);
                        },
                      ),
                    ],
                  ),
                  const SizedBox(height: AppSpace.md),
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 4),
                    child: Text(
                      _isolate
                          ? '隔离模式：换一个会话标识等于开一段空白对话，只影响这台设备。'
                          : '共享模式：默认所有接入的设备都进同一个会话，聊天记录和上下文是同一份。'
                              '这时右上角没有「新对话」——换会话会让别的设备对不上。',
                      style: Theme.of(context)
                          .textTheme
                          .labelSmall
                          ?.copyWith(color: dsh.textTertiary, height: 1.6),
                    ),
                  ),
                  if (_hint.isNotEmpty) ...[
                    const SizedBox(height: AppSpace.lg),
                    _Hint(text: _hint, ok: _ok),
                  ],
                  const SizedBox(height: AppSpace.xxl),
                  Row(
                    children: [
                      Expanded(
                        child: _Button(
                          label: _busy ? '处理中…' : '测试连接',
                          onTap: _busy ? null : _test,
                          primary: false,
                        ),
                      ),
                      const SizedBox(width: AppSpace.md),
                      Expanded(
                        child: _Button(
                          label: '保存并连接',
                          onTap: () {
                            if (_commit()) {
                              HapticFeedback.selectionClick();
                              Navigator.of(context).pop();
                            }
                          },
                        ),
                      ),
                    ],
                  ),
                ],
              ),
              Positioned(left: 0, right: 0, top: 0, child: _TopBar(title: '连接设置')),
            ],
          ),
        ),
      ),
    );
  }
}

class _TopBar extends StatelessWidget {
  const _TopBar({required this.title});

  final String title;

  @override
  Widget build(BuildContext context) {
    final top = MediaQuery.paddingOf(context).top;
    final colors = Theme.of(context).colorScheme;
    return Padding(
      padding: EdgeInsets.fromLTRB(12, top, 12, 0),
      child: SizedBox(
        height: 56,
        child: Row(
          children: [
            SizedBox.square(
              dimension: 44,
              child: GlassSurface(
                tier: GlassTier.chrome,
                circle: true,
                grouped: true,
                onTap: () => Navigator.of(context).maybePop(),
                child: Center(child: Icon(AppIcons.back, size: 22, color: colors.onSurface)),
              ),
            ),
            const SizedBox(width: AppSpace.sm),
            GlassSurface(
              tier: GlassTier.chrome,
              stadium: true,
              grouped: true,
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                child: Text(
                  title,
                  style: Theme.of(context).textTheme.titleSmall?.copyWith(fontSize: 15, height: 1.3),
                ),
              ),
            ),
          ],
        ),
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
      padding: const EdgeInsets.fromLTRB(4, 0, 4, 8),
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

class _Section extends StatelessWidget {
  const _Section({required this.children});

  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return GlassSurface(
      tier: GlassTier.card,
      borderRadius: AppShape.radiusLg,
      child: Column(children: children),
    );
  }
}

class _Divider extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(left: 16),
      child: Divider(
        height: 1,
        thickness: 1,
        color: Theme.of(context).colorScheme.outlineVariant.withValues(alpha: 0.35),
      ),
    );
  }
}

class _Field extends StatelessWidget {
  const _Field({
    required this.label,
    required this.controller,
    required this.hint,
    this.obscure = false,
  });

  final String label;
  final TextEditingController controller;
  final String hint;
  final bool obscure;

  @override
  Widget build(BuildContext context) {
    final dsh = DshColors.of(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
      child: Row(
        children: [
          SizedBox(
            width: 74,
            child: Text(
              label,
              style: Theme.of(context).textTheme.labelMedium?.copyWith(color: dsh.textTertiary),
            ),
          ),
          Expanded(
            child: TextField(
              controller: controller,
              obscureText: obscure,
              textAlign: TextAlign.end,
              style: Theme.of(context).textTheme.bodyMedium,
              cursorColor: Theme.of(context).colorScheme.primary,
              decoration: InputDecoration(
                isDense: true,
                filled: false,
                border: InputBorder.none,
                enabledBorder: InputBorder.none,
                focusedBorder: InputBorder.none,
                disabledBorder: InputBorder.none,
                hintText: hint,
                hintStyle: Theme.of(context).textTheme.bodyMedium?.copyWith(color: dsh.textDisabled),
                contentPadding: const EdgeInsets.symmetric(vertical: 4),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _Action extends StatelessWidget {
  const _Action({required this.icon, required this.label, required this.onTap});

  final IconData icon;
  final String label;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final dsh = DshColors.of(context);
    return Pressable(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
        child: Row(
          children: [
            Icon(icon, size: 17, color: dsh.textTertiary),
            const SizedBox(width: AppSpace.sm),
            Expanded(child: Text(label, style: Theme.of(context).textTheme.bodySmall)),
            Icon(AppIcons.disclosure, size: 14, color: dsh.textDisabled),
          ],
        ),
      ),
    );
  }
}

class _Switch extends StatelessWidget {
  const _Switch({
    required this.label,
    required this.detail,
    required this.value,
    required this.onChanged,
  });

  final String label;
  final String detail;
  final bool value;
  final ValueChanged<bool> onChanged;

  @override
  Widget build(BuildContext context) {
    final dsh = DshColors.of(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 12, 12),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(label, style: Theme.of(context).textTheme.bodyMedium),
                const SizedBox(height: 4),
                Text(
                  detail,
                  style: Theme.of(context)
                      .textTheme
                      .labelSmall
                      ?.copyWith(color: dsh.textTertiary, height: 1.5),
                ),
              ],
            ),
          ),
          Switch.adaptive(
            value: value,
            onChanged: onChanged,
            activeTrackColor: Theme.of(context).colorScheme.primary,
          ),
        ],
      ),
    );
  }
}

class _Hint extends StatelessWidget {
  const _Hint({required this.text, required this.ok});

  final String text;
  final bool ok;

  @override
  Widget build(BuildContext context) {
    final dsh = DshColors.of(context);
    final color = ok ? dsh.success : dsh.warning;
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.only(top: 1),
          child: Icon(ok ? AppIcons.done : AppIcons.warning, size: 15, color: color),
        ),
        const SizedBox(width: AppSpace.sm),
        Expanded(
          child: Text(
            text,
            style: Theme.of(context).textTheme.labelMedium?.copyWith(color: color, height: 1.5),
          ),
        ),
      ],
    );
  }
}

class _Button extends StatelessWidget {
  const _Button({required this.label, required this.onTap, this.primary = true});

  final String label;
  final VoidCallback? onTap;
  final bool primary;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    final enabled = onTap != null;
    return Pressable(
      onTap: onTap,
      enabled: enabled,
      child: Container(
        height: 48,
        alignment: Alignment.center,
        decoration: ShapeDecoration(
          color: primary
              ? (enabled ? colors.primary : colors.primary.withValues(alpha: 0.4))
              : colors.onSurface.withValues(alpha: 0.06),
          shape: AppShape.dialog,
        ),
        child: Text(
          label,
          style: Theme.of(context).textTheme.labelLarge?.copyWith(
                color: primary ? colors.onPrimary : colors.onSurface,
              ),
        ),
      ),
    );
  }
}
