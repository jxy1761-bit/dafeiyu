import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../astrbot/astrbot_client.dart';
import '../../astrbot/discovery.dart';
import '../../astrbot/models.dart';
import '../../ui/common/pressable.dart';
import '../../ui/common/status_dot.dart';
import '../../ui/theme/theme.dart';
import '../prefs.dart';
import '../settings_page.dart';
import 'chat_controller.dart';
import 'chat_drawer.dart';
import 'chat_header.dart';
import 'chat_list.dart';
import 'composer.dart';
import 'empty_state.dart';
import '../media/attachment_store.dart';
import '../media/media_bits.dart';
import '../media/stickers.dart';
import '../media/voice_recorder.dart';
import '../notifications.dart';

/// 聊天页。
///
/// 骨架照搬 dsh-Go 的 `ChatScreen`:**不是一个 Column**,而是一个
/// `CustomMultiChildLayout`,五个槽各就各位 ——
///
/// ```
/// ┌──────── header ────────┐   浮在最上层,内容是滚到它下面去的
/// │                        │
/// │          body          │   列表;下沿伸到输入条上沿以下 6(看不见裁切)
/// │                        │
/// ├────────────────────────┤
/// │         veil           │   从输入条上沿往下的实心背景,和 body 同帧排好
/// ├────────────────────────┤
/// │        composer        │   先量它,再由它算 body 的高度
/// └────────────────────────┘
/// ```
class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key});

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

enum _Slot { body, veil, header, composer, palette }

class _ChatScreenState extends State<ChatScreen> with WidgetsBindingObserver {
  AstrBotClient? _client;
  ChatController? _chat;

  final _input = TextEditingController();
  final _focus = FocusNode();
  final _scaffoldKey = GlobalKey<ScaffoldState>();
  final _veilHeight = ValueNotifier<double>(0);
  final _scrolledUnder = ValueNotifier<double>(0);
  bool _askedSetup = false;

  /// 「新对话」按过之后不要再塞演示数据。
  bool _startBlank = false;

  /// 正在扫局域网找服务器。
  bool _finding = false;

  /// 最近一次找到/用上的地址(给顶栏状态行显示)。
  String? _server;

  /// 待发附件(还没上传,发送时才传)。
  final List<DraftPart> _drafts = [];
  bool _stickerOpen = false;
  bool _uploading = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    WidgetsBinding.instance.addPostFrameCallback((_) => _boot());
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // 从后台回来(可能刚换了 WiFi):地址不通就重新找。
    if (state != AppLifecycleState.resumed || _demo) return;
    final base = _server ?? Prefs.i.baseUrl;
    if (base.isEmpty) return;
    Discovery.alive(base, timeout: const Duration(milliseconds: 900)).then((ok) {
      if (!ok && mounted) _connect();
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _chat?.removeListener(_onChat);
    _chat?.dispose();
    _client?.close();
    _input.dispose();
    _focus.dispose();
    _veilHeight.dispose();
    _scrolledUnder.dispose();
    super.dispose();
  }

  /// 验收包:不读配置、不联网,直接摆一段对话看排版。
  static const _demo = bool.fromEnvironment('DEMO');

  Future<void> _boot() async {
    if (_demo) {
      _demoBoot();
      return;
    }
    // 账号密码都没有,先让人填;有账号但没地址,直接自动找。
    if (Prefs.i.username.isEmpty || Prefs.i.password.isEmpty) {
      unawaited(MessageNotifications.stopWatch());
      if (!_askedSetup) {
        _askedSetup = true;
        _openSettings(firstRun: true);
      }
      return;
    }
    await _connect();
  }

  void _onChat() {
    if (mounted) setState(() {});
  }

  Future<void> _demoBoot() async {
    _chat?.removeListener(_onChat);
    _chat?.dispose();
    _client?.close();
    final client = AstrBotClient(baseUrl: 'http://127.0.0.1:6185', username: 'demo', password: 'demo');
    final chat = ChatController(client: client, sessionId: 'demo');
    _client = client;
    _chat = chat;
    chat.addListener(_onChat);
    if (!_startBlank) {
      chat.seedDemo();
      await _seedDemoMedia(chat);
    } else {
      chat.markConnected();
    }
    if (mounted) setState(() {});
  }

  /// 验收包专用:塞一张图和一段静音语音,让媒体气泡也有得看。
  Future<void> _seedDemoMedia(ChatController chat) async {
    final png = (await rootBundle.load('assets/dafeiyu/avatar.png')).buffer.asUint8List();
    Attachments.put('demo-img', png);
    final wav = _silenceWav(seconds: 3);
    Attachments.put('demo-voice', wav);

    chat.items.add(
      ChatItem(
        id: 'd6',
        role: ChatRole.her,
        time: DateTime.now().subtract(const Duration(minutes: 3)),
        parts: [
          const TextPart('给你看个东西，顺手把尾巴上的水甩你一脸。'),
          const MediaPart(kind: MediaKind.image, attachmentId: 'demo-img'),
        ],
      ),
    );
    chat.items.add(
      ChatItem(
        id: 'd7',
        role: ChatRole.me,
        time: DateTime.now().subtract(const Duration(minutes: 2)),
        parts: [
          const MediaPart(kind: MediaKind.record, attachmentId: 'demo-voice', bytes: 96044),
        ],
      ),
    );
    final note = utf8.encode('大肥鱼 · 验收包里的示例文件\n这是从 App 存进系统下载目录的。');
    Attachments.put('demo-file', note);
    chat.items.add(
      ChatItem(
        id: 'd8a',
        role: ChatRole.her,
        time: DateTime.now().subtract(const Duration(minutes: 2)),
        parts: [
          const MediaPart(
            kind: MediaKind.file,
            attachmentId: 'demo-file',
            name: '大肥鱼说明.txt',
            bytes: 96,
            mime: 'text/plain',
          ),
        ],
      ),
    );
    chat.items.add(
      ChatItem(
        id: 'd8',
        role: ChatRole.her,
        time: DateTime.now().subtract(const Duration(minutes: 1)),
        parts: [
          const TextPart('收到啦，你的声音我听着呢。'),
        ],
      ),
    );
    chat.markConnected();
  }

  /// 造一段静音 WAV(16k / 16bit / 单声道),只给验收包用。
  static Uint8List _silenceWav({required int seconds}) {
    const rate = 16000;
    final samples = rate * seconds;
    final dataLen = samples * 2;
    final b = BytesBuilder();
    void str(String v) => b.add(v.codeUnits);
    void u32(int v) => b.add([v & 0xff, (v >> 8) & 0xff, (v >> 16) & 0xff, (v >> 24) & 0xff]);
    void u16(int v) => b.add([v & 0xff, (v >> 8) & 0xff]);

    str('RIFF');
    u32(36 + dataLen);
    str('WAVEfmt ');
    u32(16);
    u16(1);
    u16(1);
    u32(rate);
    u32(rate * 2);
    u16(2);
    u16(16);
    str('data');
    u32(dataLen);
    b.add(Uint8List(dataLen));
    return b.toBytes();
  }

  /// 连上(必要时先自动找到服务器)。
  ///
  /// 地址来源依次是:手填的地址 → 历史连通过的地址 → 扫当前 /24 网段。
  /// 找到之后写回 [Prefs.baseUrl],下次换回来不用再扫。
  Future<void> _connect() async {
    if (_demo) {
      _demoBoot();
      return;
    }
    _chat?.removeListener(_onChat);
    _chat?.dispose();
    _client?.close();

    final base = await _resolveBase();
    _server = base;
    if (base.isEmpty) {
      // 一个都找不到:把错误交给控制器显示,页面仍然是可用的
      if (mounted) setState(() {});
      return;
    }

    final client = AstrBotClient(
      baseUrl: base,
      username: Prefs.i.username,
      password: Prefs.i.password,
      hubUrl: Prefs.i.hubUrl,
      clientId: Prefs.i.clientId,
    );
    final chat = ChatController(client: client, sessionId: Prefs.i.sessionId);
    _client = client;
    _chat = chat;
    Attachments.client = client;
    chat.addListener(_onChat);
    chat.open();
    if (Prefs.i.realtime) {
      unawaited(MessageNotifications.startWatch(
        baseUrl: base,
        hubUrl: Prefs.i.hubUrl,
        username: Prefs.i.username,
        password: Prefs.i.password,
        sessionId: Prefs.i.sessionId,
      ));
    } else {
      unawaited(MessageNotifications.stopWatch());
    }
    if (mounted) setState(() {});
  }

  /// 手填的地址 → 历史地址 → 扫网段,返回一个活着的 base url。
  Future<String> _resolveBase() async {
    final saved = Prefs.i.baseUrl.trim();

    // 1. 手填/上次用的地址还通,直接用
    if (saved.isNotEmpty && await Discovery.alive(saved)) {
      await Prefs.i.rememberHost(saved);
      return saved;
    }
    if (!Prefs.i.autoDiscover) return saved;

    if (mounted) setState(() => _finding = true);
    try {
      // 2. 之前连通过的地址,挨个试(换回老 WiFi 时最快)
      for (final host in Prefs.i.knownHosts) {
        if (host == saved) continue;
        if (await Discovery.alive(host)) {
          Prefs.i.baseUrl = host;
          await Prefs.i.rememberHost(host);
          return host;
        }
      }
      // 3. 扫当前 /24,命中的再用账号验一遍,避免认错人
      final hits = await Discovery.scan(port: _portOf(saved));
      for (final host in hits.take(6)) {
        if (await _login(host)) {
          Prefs.i.baseUrl = host;
          await Prefs.i.rememberHost(host);
          return host;
        }
      }
    } finally {
      if (mounted) setState(() => _finding = false);
    }
    return saved;
  }

  int _portOf(String url) {
    final uri = Uri.tryParse(url);
    if (uri != null && uri.hasPort) return uri.port;
    return Discovery.defaultPort;
  }

  Future<bool> _login(String base) async {
    final probe = AstrBotClient(
      baseUrl: base,
      username: Prefs.i.username,
      password: Prefs.i.password,
    );
    try {
      await probe.login(force: true).timeout(const Duration(seconds: 6));
      return true;
    } catch (_) {
      return false;
    } finally {
      probe.close();
    }
  }

  void _restart() {
    _input.clear();
    _scrolledUnder.value = 0;
    _connect();
  }

  Future<void> _openSettings({bool firstRun = false}) async {
    _focus.unfocus();
    await Navigator.of(context).push(
      PageRouteBuilder(
        pageBuilder: (_, __, ___) => const SettingsPage(),
        transitionsBuilder: (context, anim, secondary, child) =>
            FadeTransition(opacity: anim, child: child),
        transitionDuration: AppMotion.of(context, AppMotion.push),
      ),
    );
    if (!mounted) return;
    if (Prefs.i.configured) {
      _restart();
    } else {
      // 没配好就直接退出,别在这里循环推页面:空页面上还能再从 ≡ 进去。
      setState(() => _askedSetup = false);
    }
  }

  void _newChat() {
    // 只有开了会话隔离才有「新对话」:共享模式下换 id 会把别的设备甩下。
    if (!Prefs.i.isolateSession) return;
    _startBlank = true;
    Prefs.i.deviceSessionId = 'device-${DateTime.now().millisecondsSinceEpoch % 1000000}';
    _restart();
    if (!_demo) {
      ScaffoldMessenger.of(context)
        ..hideCurrentSnackBar()
        ..showSnackBar(
          const SnackBar(content: Text('已开始新对话'), duration: Duration(milliseconds: 1400)),
        );
    }
  }

  Future<void> _send() async {
    var chat = _chat;
    final text = _input.text.trim();
    if (chat == null || chat.running || _uploading) return;
    if (text.isEmpty && _drafts.isEmpty) return;

    // 换了网:先在当前网络里把服务器重找一遍,再发
    if (chat.conn == ChatConn.offline) {
      await _connect();
      chat = _chat;
      if (chat == null) return;
    }

    final parts = <MsgPart>[];
    if (_drafts.isNotEmpty) {
      final client = _client;
      if (client == null) return;
      setState(() => _uploading = true);
      try {
        for (final d in _drafts) {
          final part = await client.upload(
            bytes: d.bytes,
            filename: d.filename,
            kind: d.kind,
            mime: d.mime,
            duration: d.duration,
          );
          // 自己发的图先塞缓存,不用再从服务器拉回来
          if (part.isImage) Attachments.put(part.attachmentId, d.bytes);
          parts.add(part);
        }
      } catch (e) {
        if (mounted) {
          setState(() => _uploading = false);
          ScaffoldMessenger.of(context)
            ..hideCurrentSnackBar()
            ..showSnackBar(SnackBar(content: Text('上传失败：$e')));
        }
        return;
      }
      if (mounted) setState(() => _uploading = false);
      if (!mounted) return;
    }
    if (text.isNotEmpty) parts.add(TextPart(text));

    _input.clear();
    setState(() {
      _drafts.clear();
      _stickerOpen = false;
    });
    _scrolledUnder.value = 0;
    await chat.sendMessage(parts);
  }

  // ---------------------------------------------------------------- 选附件

  static String _mimeOf(String name) {
    final ext = name.contains('.') ? name.split('.').last.toLowerCase() : '';
    return switch (ext) {
      'png' => 'image/png',
      'jpg' || 'jpeg' => 'image/jpeg',
      'gif' => 'image/gif',
      'webp' => 'image/webp',
      'wav' => 'audio/wav',
      'mp3' => 'audio/mpeg',
      'm4a' => 'audio/mp4',
      'pdf' => 'application/pdf',
      _ => 'application/octet-stream',
    };
  }

  void _addDraft(Uint8List bytes, String name, MediaKind kind) {
    _drafts.add(
      DraftPart(bytes: bytes, filename: name, kind: kind, mime: _mimeOf(name)),
    );
    setState(() {});
  }

  Future<void> _pickImages() async {
    final r = await FilePicker.platform.pickFiles(
      type: FileType.image,
      allowMultiple: true,
      withData: true,
    );
    if (r == null) return;
    for (final f in r.files) {
      final bytes = f.bytes;
      if (bytes == null) continue;
      _addDraft(bytes, f.name, MediaKind.image);
    }
  }

  Future<void> _pickFiles() async {
    final r = await FilePicker.platform.pickFiles(allowMultiple: true, withData: true);
    if (r == null) return;
    for (final f in r.files) {
      final bytes = f.bytes;
      if (bytes == null) continue;
      _addDraft(bytes, f.name, MediaKind.file);
    }
  }

  Future<void> _recordVoice() async {
    final voice = await recordVoiceSheet(context);
    if (voice == null || !mounted) return;
    _drafts.add(
      DraftPart(
        bytes: voice.bytes,
        filename: 'voice_${DateTime.now().millisecondsSinceEpoch}.wav',
        kind: MediaKind.record,
        mime: 'audio/wav',
        duration: voice.seconds,
      ),
    );
    setState(() {});
  }

  /// 表情包点一下就发,不用先进草稿。
  Future<void> _sendSticker(File file) async {
    final client = _client;
    var chat = _chat;
    if (client == null || chat == null) return;
    setState(() => _uploading = true);
    try {
      final bytes = await file.readAsBytes();
      final name = file.uri.pathSegments.last;
      final part = await client.upload(
        bytes: bytes,
        filename: name,
        kind: MediaKind.image,
        mime: _mimeOf(name),
      );
      Attachments.put(part.attachmentId, bytes);
      setState(() {
        _uploading = false;
        _stickerOpen = false;
      });
      await chat.sendMessage([part]);
    } catch (e) {
      if (!mounted) return;
      setState(() => _uploading = false);
      ScaffoldMessenger.of(context)
        ..hideCurrentSnackBar()
        ..showSnackBar(SnackBar(content: Text('发送失败：$e')));
    }
  }

  Future<void> _openAttach() async {
    _focus.unfocus();
    final choice = await showModalBottomSheet<String>(
      context: context,
      backgroundColor: Colors.transparent,
      barrierColor: Colors.black.withValues(alpha: 0.42),
      builder: (context) => _AttachSheet(
        onPick: (v) => Navigator.of(context).pop(v),
      ),
    );
    if (!mounted || choice == null) return;
    switch (choice) {
      case 'image':
        await _pickImages();
      case 'file':
        await _pickFiles();
      case 'voice':
        await _recordVoice();
      case 'sticker':
        setState(() => _stickerOpen = !_stickerOpen);
    }
  }

  void _stop() => _chat?.stop();

  void _suggestion(String text) {
    _input
      ..text = text
      ..selection = TextSelection.collapsed(offset: text.length);
    _focus.requestFocus();
  }

  // ------------------------------------------------------------------ 抽屉

  void _toast(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(text), duration: const Duration(seconds: 2)));
  }

  /// 二次确认。返回 true 才是「真要删」。
  Future<bool> _confirm({
    required String title,
    required String body,
    required String confirmLabel,
    bool danger = false,
  }) async {
    final ok = await showModalBottomSheet<bool>(
      context: context,
      backgroundColor: Colors.transparent,
      barrierColor: Colors.black.withValues(alpha: 0.5),
      builder: (context) => _ConfirmSheet(
        title: title,
        body: body,
        confirmLabel: confirmLabel,
        danger: danger,
      ),
    );
    return ok == true;
  }

  /// 清空聊天记录:删消息 + 附件,会话 id 保留。
  Future<void> _clearChat() async {
    final chat = _chat;
    if (chat == null) return;
    final shared = !Prefs.i.isolateSession;
    final ok = await _confirm(
      title: '清空聊天记录',
      body: shared
          ? '这段共享会话里的所有消息、历史和附件文件都会从服务器上删掉，'
              '所有设备都会看到被清空。清空后会话还在，可以接着聊。'
          : '这段会话里的所有消息、历史和附件文件都会从服务器上删掉，无法恢复。'
              '清空后会话还在，可以接着聊。',
      confirmLabel: '清空',
    );
    if (!ok || !mounted) return;
    try {
      if (_demo) {
        chat.items.clear();
        chat.markConnected();
      } else {
        await chat.wipe();
      }
      _toast('已清空');
    } catch (e) {
      _toast('清空失败：$e');
    }
  }

  /// 删除整个会话:连附件一起清掉;隔离模式下本机再换一个新的会话 id。
  Future<void> _deleteChat() async {
    final chat = _chat;
    if (chat == null) return;
    final isolated = Prefs.i.isolateSession;
    final ok = await _confirm(
      title: isolated ? '删除此会话' : '删除共享会话',
      body: isolated
          ? '整个会话连同里面的消息、历史和附件文件都会从服务器上删掉，无法恢复。'
              '本机会换到一个新的空会话。'
          : '这是所有设备共用的会话：删掉之后，所有人的聊天记录和附件都会消失，无法恢复。'
              '本机会留在同一个会话 id 上，下次说话会重新开一段。',
      confirmLabel: '删除',
      danger: true,
    );
    if (!ok || !mounted) return;
    try {
      if (_demo) {
        chat.items.clear();
        chat.markConnected();
      } else {
        await chat.wipe();
      }
      if (!mounted) return;
      if (isolated) {
        Prefs.i.deviceSessionId = 'device-${DateTime.now().millisecondsSinceEpoch % 1000000}';
        _startBlank = true;
        _restart();
      }
      _toast('已删除');
    } catch (e) {
      _toast('删除失败：$e');
    }
  }

  void _openMenu() {
    _focus.unfocus();
    _scaffoldKey.currentState?.openDrawer();
  }

  /// 标题胶囊:看这段对话的细节(和 dsh-Go 点标题看详情一致)。
  Future<void> _showDetails() async {
    final chat = _chat;
    final uri = Uri.tryParse(Prefs.i.baseUrl);
    final rows = <(String, String)>[
      ('服务器', (_server ?? Prefs.i.baseUrl).isEmpty
          ? '未配置'
          : (uri == null || uri.host.isEmpty ? (_server ?? Prefs.i.baseUrl) : '${uri.scheme}://${uri.host}:${uri.port}')),
      ('会话', Prefs.i.isolateSession ? '${Prefs.i.sessionId}（仅本机）' : '${Prefs.i.sessionId}（所有设备共享）'),
      ('状态', switch (chat?.conn) {
        ChatConn.connected => '已连接',
        ChatConn.connecting => '连接中',
        ChatConn.offline => '未连接',
        null => '—',
      }),
      ('消息', '${chat?.items.length ?? 0} 条'),
    ];
    await showModalBottomSheet<void>(
      context: context,
      backgroundColor: Colors.transparent,
      barrierColor: Colors.black.withValues(alpha: 0.42),
      builder: (context) => _DetailsSheet(rows: rows),
    );
  }

  // ------------------------------------------------------------------ 装配

  @override
  Widget build(BuildContext context) {
    final chat = _chat;
    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: dshSystemOverlay(Theme.of(context).brightness),
      child: AppBackdrop(
        child: Scaffold(
          key: _scaffoldKey,
          backgroundColor: Colors.transparent,
          resizeToAvoidBottomInset: true,
          drawer: chat == null ? null : _drawer(chat),
          body: chat == null
              ? const SizedBox.expand()
              : CustomMultiChildLayout(
                  delegate: _ChatLayout(_veilHeight),
                  children: [
                    LayoutId(id: _Slot.body, child: _body(chat)),
                    LayoutId(
                      id: _Slot.veil,
                      child: BackdropVeil(solidHeight: _veilHeight),
                    ),
                    LayoutId(id: _Slot.header, child: _header(chat)),
                    LayoutId(id: _Slot.composer, child: _composerSlot(chat)),
                    LayoutId(id: _Slot.palette, child: const SizedBox.shrink()),
                  ],
                ),
        ),
      ),
    );
  }

  Widget _body(ChatController chat) {
    final topInset = ChatHeader.heightOf(context);
    if (chat.empty) {
      return EmptyState(
        topInset: topInset,
        connected: chat.conn == ChatConn.connected,
        onSuggestion: _suggestion,
      );
    }
    return ChatList(
      chat: chat,
      topInset: topInset,
      bottomInset: _ChatLayout.overlap,
      scrolledUnder: _scrolledUnder,
      onRetry: () => chat.reconnect(),
    );
  }

  Widget _header(ChatController chat) {
    if (_finding) {
      return ChatHeader(
        title: '大肥鱼',
        status: '正在寻找服务器…',
        tone: StatusTone.busy,
        detail: null,
        scrolledUnder: chat.empty ? null : _scrolledUnder,
        onOpenMenu: _openMenu,
        onNewChat: Prefs.i.isolateSession ? _newChat : null,
        onTitleTap: _showDetails,
      );
    }
    final (label, tone) = switch (chat.conn) {
      ChatConn.connecting => ('连接中…', StatusTone.busy),
      ChatConn.offline => ('未连接', StatusTone.idle),
      ChatConn.connected =>
        chat.running ? ('正在回复…', StatusTone.ok) : ('已连接', StatusTone.ok),
    };
    return ChatHeader(
      title: '大肥鱼',
      status: label,
      tone: tone,
      detail: _hostLabel(),
      scrolledUnder: chat.empty ? null : _scrolledUnder,
      onOpenMenu: _openMenu,
      onNewChat: Prefs.i.isolateSession ? _newChat : null,
      onTitleTap: _showDetails,
    );
  }

  Widget _drawer(ChatController chat) {
    final (label, tone) = switch (chat.conn) {
      ChatConn.connecting => ('连接中…', StatusTone.busy),
      ChatConn.offline => ('未连接', StatusTone.idle),
      ChatConn.connected =>
        chat.running ? ('正在回复…', StatusTone.ok) : ('已连接 · ${_hostLabel() ?? '本机'}', StatusTone.ok),
    };
    void closeThen(VoidCallback action) {
      _scaffoldKey.currentState?.closeDrawer();
      // 等抽屉收回去再弹确认框,别两层挡在一起
      Future<void>.delayed(AppMotion.quick, action);
    }

    return ChatDrawer(
      title: '大肥鱼',
      subtitle: label,
      tone: tone,
      messageCount: chat.items.length,
      sessionLabel: Prefs.i.isolateSession ? Prefs.i.sessionId : '共享会话',
      shared: !Prefs.i.isolateSession,
      onClear: chat.items.isEmpty ? null : () => closeThen(_clearChat),
      onDelete: () => closeThen(_deleteChat),
      onNewChat: Prefs.i.isolateSession ? () => closeThen(_newChat) : null,
      onReconnect: () => closeThen(() {
        chat.clearError();
        chat.reconnect();
      }),
      onSettings: () => closeThen(_openSettings),
      onClose: () => _scaffoldKey.currentState?.closeDrawer(),
      liveConnected: chat.liveConnected,
    );
  }

  String? _hostLabel() {
    final url = Prefs.i.baseUrl;
    if (url.isEmpty) return null;
    final uri = Uri.tryParse(url);
    if (uri == null || uri.host.isEmpty) return null;
    return uri.host;
  }

  Widget _composerSlot(ChatController chat) {
    return SafeArea(
      top: false,
      minimum: const EdgeInsets.only(bottom: 8),
      child: Padding(
        // 与顶栏两颗键一致的 12。
        padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: AppSpace.floatingWidth),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                // 胶囊条:出错时这里是唯一要说的那一句,平时不占高度。
                AnimatedSize(
                  duration: AppMotion.of(context, AppMotion.standard),
                  curve: AppMotion.std,
                  alignment: Alignment.bottomCenter,
                  child: chat.error == null
                      ? const SizedBox(width: double.infinity)
                      : Padding(
                          padding: const EdgeInsets.only(bottom: AppSpace.sm),
                          child: _ErrorCapsule(
                            message: chat.error!,
                            onRetry: () {
                              chat.clearError();
                              _connect();
                            },
                            onDismiss: chat.clearError,
                          ),
                        ),
                ),
                // 表情面板:从输入条上方展开
                AnimatedSize(
                  duration: AppMotion.of(context, AppMotion.standard),
                  curve: AppMotion.std,
                  alignment: Alignment.bottomCenter,
                  child: _stickerOpen
                      ? Padding(
                          padding: const EdgeInsets.only(bottom: AppSpace.sm),
                          child: GlassSurface(
                            tier: GlassTier.card,
                            borderRadius: AppShape.radiusLg,
                            padding: const EdgeInsets.fromLTRB(12, 10, 12, 8),
                            child: StickerPanel(onPick: _sendSticker),
                          ),
                        )
                      : const SizedBox(width: double.infinity),
                ),
                Composer(
                  controller: _input,
                  focusNode: _focus,
                  running: chat.running,
                  connected: chat.conn == ChatConn.connected,
                  maxLines: _linesFor(context),
                  hasDrafts: _drafts.isNotEmpty,
                  hint: chat.conn == ChatConn.connected ? '说点什么…' : '还没连上服务器',
                  onSend: _send,
                  onStop: _stop,
                  leading: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      _KeyIcon(
                        icon: AppIcons.plus,
                        onTap: _uploading ? null : _openAttach,
                        busy: _uploading,
                      ),
                      const SizedBox(width: 2),
                      _KeyIcon(
                        icon: AppIcons.smiley,
                        onTap: () => setState(() => _stickerOpen = !_stickerOpen),
                        active: _stickerOpen,
                      ),
                    ],
                  ),
                  tray: _drafts.isEmpty ? null : _DraftTray(
                    drafts: _drafts,
                    onRemove: (i) => setState(() => _drafts.removeAt(i)),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  /// 输入框最多几行:横屏 / 键盘弹起时少给一点,别把列表挤没了。
  static int _linesFor(BuildContext context) {
    final h = MediaQuery.sizeOf(context).height;
    final keyboard = MediaQuery.viewInsetsOf(context).bottom;
    if (keyboard > 0 || h < 520) return 3;
    return 6;
  }
}

/// 五个槽的排法。先量输入条,再由它反推列表高度 —— 这一层排完,遮罩就
/// 按新高度画,同一帧里严丝合缝。
class _ChatLayout extends MultiChildLayoutDelegate {
  _ChatLayout(this.veilHeight);

  /// 列表视口伸到输入条上沿以下的距离。
  static const overlap = 6.0;

  final ValueNotifier<double> veilHeight;

  @override
  void performLayout(Size size) {
    final width = size.width;
    // 高度不设限:每个槽自己收缩包住内容,和底部锚定的 Positioned 一样。
    final along = BoxConstraints(minWidth: width, maxWidth: width);

    final composer = layoutChild(_Slot.composer, along);
    final top = size.height - composer.height;
    positionChild(_Slot.composer, Offset(0, top));
    veilHeight.value = math.max(0, composer.height - overlap);

    layoutChild(_Slot.body, BoxConstraints.tight(Size(width, math.max(0, top + overlap))));
    positionChild(_Slot.body, Offset.zero);

    layoutChild(_Slot.veil, BoxConstraints.tight(size));
    positionChild(_Slot.veil, Offset.zero);

    layoutChild(_Slot.header, along);
    positionChild(_Slot.header, Offset.zero);

    final paletteWidth = math.max(0.0, width - 2 * AppSpace.floatInset);
    final palette =
        layoutChild(_Slot.palette, BoxConstraints(minWidth: paletteWidth, maxWidth: paletteWidth));
    positionChild(_Slot.palette, Offset(AppSpace.floatInset, top - 6 - palette.height));
  }

  @override
  bool shouldRelayout(_ChatLayout oldDelegate) => oldDelegate.veilHeight != veilHeight;
}

/// 出错时的胶囊:一句原因 + 「重试」,右边一颗 ×。
class _ErrorCapsule extends StatelessWidget {
  const _ErrorCapsule({required this.message, required this.onRetry, required this.onDismiss});

  final String message;
  final VoidCallback onRetry;
  final VoidCallback onDismiss;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final dsh = DshColors.of(context);
    return GlassSurface(
      tier: GlassTier.chrome,
      borderRadius: AppShape.radiusLg,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 8, 8, 8),
        child: Row(
          children: [
            Icon(AppIcons.warning, size: 16, color: dsh.warning),
            const SizedBox(width: AppSpace.sm),
            Expanded(
              child: Text(
                message,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: theme.textTheme.labelMedium?.copyWith(color: dsh.warning, height: 1.4),
              ),
            ),
            Pressable(
              onTap: onRetry,
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                child: Text('重试', style: theme.textTheme.labelLarge),
              ),
            ),
            Pressable(
              onTap: onDismiss,
              semanticLabel: '关闭',
              child: Padding(
                padding: const EdgeInsets.all(6),
                child: Icon(AppIcons.close, size: 16, color: dsh.textTertiary),
              ),
            ),
          ],
        ),
      ),
    );
  }
}


/// 标题胶囊点开的详情:一张卡,四行键值。没有可点的操作,只是把「我在连
/// 哪台机器、用的是哪个会话」摆出来 —— 和 dsh-Go 的对话详情同一个位置。
class _DetailsSheet extends StatelessWidget {
  const _DetailsSheet({required this.rows});

  final List<(String, String)> rows;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final dsh = DshColors.of(context);
    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
        child: GlassSurface(
          tier: GlassTier.sheet,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 10, 16, 16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Center(
                  child: Container(
                    width: 36,
                    height: 4,
                    decoration: BoxDecoration(
                      color: dsh.textTertiary.withValues(alpha: 0.5),
                      borderRadius: BorderRadius.circular(2),
                    ),
                  ),
                ),
                const SizedBox(height: 14),
                Text('对话详情', style: theme.textTheme.titleSmall),
                const SizedBox(height: 10),
                for (final (k, v) in rows)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 7),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        SizedBox(
                          width: 64,
                          child: Text(
                            k,
                            style: theme.textTheme.labelMedium?.copyWith(color: dsh.textTertiary),
                          ),
                        ),
                        Expanded(
                          child: SelectableText(
                            v,
                            style: theme.textTheme.bodyMedium,
                          ),
                        ),
                      ],
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

/// 输入条左边那种 36 的小键。
class _KeyIcon extends StatelessWidget {
  const _KeyIcon({required this.icon, required this.onTap, this.active = false, this.busy = false});

  final IconData icon;
  final VoidCallback? onTap;
  final bool active;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    final dsh = DshColors.of(context);
    return Semantics(
      button: true,
      child: Pressable(
        onTap: onTap,
        child: Container(
          width: 36,
          height: 36,
          decoration: BoxDecoration(
            color: active ? colors.primary.withValues(alpha: 0.16) : Colors.transparent,
            shape: BoxShape.circle,
          ),
          child: busy
              ? const Padding(
                  padding: EdgeInsets.all(10),
                  child: CircularProgressIndicator(strokeWidth: 2),
                )
              : Icon(
                  icon,
                  size: 20,
                  color: active ? colors.primary : (onTap == null ? dsh.textDisabled : colors.onSurface),
                ),
        ),
      ),
    );
  }
}

/// 「+」打开的那张纸:图片 / 文件 / 语音 / 表情包。
class _AttachSheet extends StatelessWidget {
  const _AttachSheet({required this.onPick});

  final ValueChanged<String> onPick;

  @override
  Widget build(BuildContext context) {
    final items = <(String, IconData, String, String)>[
      ('image', AppIcons.gallery, '图片', '从相册选，可多张'),
      ('file', AppIcons.file, '文件', '任意格式'),
      ('voice', AppIcons.voice, '语音', '录一段发过去'),
      ('sticker', AppIcons.smiley, '表情包', '点一下直接发'),
    ];
    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
        child: GlassSurface(
          tier: GlassTier.sheet,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 18, 12, 14),
            child: Row(
              children: [
                for (final (id, icon, label, hint) in items)
                  Expanded(
                    child: Pressable(
                      onTap: () => onPick(id),
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Container(
                            width: 48,
                            height: 48,
                            decoration: BoxDecoration(
                              color: Theme.of(context).colorScheme.onSurface.withValues(alpha: 0.07),
                              borderRadius: BorderRadius.circular(AppShape.md),
                            ),
                            child: Icon(icon, size: 22),
                          ),
                          const SizedBox(height: 8),
                          Text(label, style: Theme.of(context).textTheme.labelLarge),
                          const SizedBox(height: 2),
                          Text(
                            hint,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: Theme.of(context).textTheme.labelSmall?.copyWith(
                                  color: DshColors.of(context).textTertiary,
                                ),
                          ),
                        ],
                      ),
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

/// 待发附件:一排缩略图,右上角一颗 × 去掉。
class _DraftTray extends StatelessWidget {
  const _DraftTray({required this.drafts, required this.onRemove});

  final List<DraftPart> drafts;
  final ValueChanged<int> onRemove;

  @override
  Widget build(BuildContext context) {
    final dsh = DshColors.of(context);
    return SizedBox(
      height: 64,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        itemCount: drafts.length,
        separatorBuilder: (_, __) => const SizedBox(width: AppSpace.sm),
        itemBuilder: (context, i) {
          final d = drafts[i];
          return Stack(
            clipBehavior: Clip.none,
            children: [
              ClipRRect(
                borderRadius: BorderRadius.circular(AppShape.sm),
                child: SizedBox(
                  width: 64,
                  height: 64,
                  child: switch (d.kind) {
                    MediaKind.image => Image.memory(d.bytes, fit: BoxFit.cover),
                    MediaKind.record => _DraftTile(icon: AppIcons.voice, label: '语音'),
                    MediaKind.file => _DraftTile(icon: AppIcons.file, label: '文件'),
                  },
                ),
              ),
              Positioned(
                top: -6,
                right: -6,
                child: Pressable(
                  onTap: () => onRemove(i),
                  child: Container(
                    width: 20,
                    height: 20,
                    decoration: BoxDecoration(
                      color: Theme.of(context).colorScheme.surface,
                      shape: BoxShape.circle,
                      border: Border.all(color: dsh.textTertiary.withValues(alpha: 0.3)),
                    ),
                    child: Icon(AppIcons.close, size: 12, color: dsh.textTertiary),
                  ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}

class _DraftTile extends StatelessWidget {
  const _DraftTile({required this.icon, required this.label});

  final IconData icon;
  final String label;

  @override
  Widget build(BuildContext context) {
    final dsh = DshColors.of(context);
    return Container(
      color: Theme.of(context).colorScheme.onSurface.withValues(alpha: 0.06),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(icon, size: 20, color: dsh.textTertiary),
          const SizedBox(height: 2),
          Text(label, style: TextStyle(fontSize: 10, color: dsh.textTertiary)),
        ],
      ),
    );
  }
}

/// 破坏性操作的二次确认:标题 + 说清后果 + 取消/确认。
class _ConfirmSheet extends StatelessWidget {
  const _ConfirmSheet({
    required this.title,
    required this.body,
    required this.confirmLabel,
    this.danger = false,
  });

  final String title;
  final String body;
  final String confirmLabel;
  final bool danger;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final dsh = DshColors.of(context);
    final colors = theme.colorScheme;
    final tone = danger ? dsh.danger : colors.primary;

    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
        child: GlassSurface(
          tier: GlassTier.sheet,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 20, 20, 16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Icon(
                      danger ? AppIcons.warning : AppIcons.info,
                      size: 18,
                      color: tone,
                    ),
                    const SizedBox(width: AppSpace.sm),
                    Text(title, style: theme.textTheme.titleSmall),
                  ],
                ),
                const SizedBox(height: AppSpace.md),
                Text(
                  body,
                  style: theme.textTheme.bodySmall?.copyWith(
                    color: dsh.textTertiary,
                    height: 1.65,
                  ),
                ),
                const SizedBox(height: AppSpace.xl),
                Row(
                  children: [
                    Expanded(
                      child: _SheetButton(
                        label: '取消',
                        background: colors.onSurface.withValues(alpha: 0.06),
                        foreground: colors.onSurface,
                        onTap: () => Navigator.of(context).pop(false),
                      ),
                    ),
                    const SizedBox(width: AppSpace.md),
                    Expanded(
                      child: _SheetButton(
                        label: confirmLabel,
                        background: tone,
                        foreground: danger ? dsh.onDanger : colors.onPrimary,
                        onTap: () => Navigator.of(context).pop(true),
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _SheetButton extends StatelessWidget {
  const _SheetButton({
    required this.label,
    required this.background,
    required this.foreground,
    required this.onTap,
  });

  final String label;
  final Color background;
  final Color foreground;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Pressable(
      onTap: onTap,
      child: Container(
        height: 46,
        alignment: Alignment.center,
        decoration: ShapeDecoration(color: background, shape: AppShape.dialog),
        child: Text(
          label,
          style: Theme.of(context).textTheme.labelLarge?.copyWith(color: foreground),
        ),
      ),
    );
  }
}
