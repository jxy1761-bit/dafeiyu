import 'dart:async';

import 'package:flutter/foundation.dart';

import '../../astrbot/astrbot_client.dart';
import '../../astrbot/models.dart';
import '../media/attachment_store.dart';

/// 连接态:顶栏状态点就靠它换颜色。
enum ChatConn { connecting, connected, offline }

/// 列表里的一行。用户消息和她的回复各是一条;回复在流式时只有一条,
/// 增量追加到同一条上(和 dsh-Go 的 row 派生一个思路)。
class ChatItem {
  ChatItem({
    required this.id,
    required this.role,
    required this.time,
    List<MsgPart>? parts,
    this.streaming = false,
  }) : parts = parts ?? <MsgPart>[];

  final String id;
  final ChatRole role;
  final DateTime time;

  /// 文字与媒体按出现顺序排;流式时一直追加到最后一个文字片段。
  final List<MsgPart> parts;

  bool streaming;

  bool get mine => role == ChatRole.me;

  String get text => parts.whereType<TextPart>().map((p) => p.text).join();

  bool get hasText => text.trim().isNotEmpty;

  bool get hasMedia => parts.any((p) => p is MediaPart);

  void appendText(String delta) {
    if (parts.isNotEmpty && parts.last is TextPart) {
      parts[parts.length - 1] = TextPart((parts.last as TextPart).text + delta);
    } else {
      parts.add(TextPart(delta));
    }
  }

  /// 非流式的一次性整段(服务端 streaming=false 时是整段而不是增量)。
  void replaceText(String whole) {
    parts.removeWhere((p) => p is TextPart);
    parts.add(TextPart(whole));
  }

  void addMedia(MediaPart part) => parts.add(part);
}

/// 会话状态:一次 [open] 拉历史,之后靠 [send] 的 SSE 和 Hub 订阅往上长。
///
/// 刻意做得很薄——界面那层只关心 items / running / conn / live 几件事。
class ChatController extends ChangeNotifier {
  ChatController({required this.client, required this.sessionId});

  final AstrBotClient client;
  final String sessionId;

  final List<ChatItem> items = [];
  ChatConn conn = ChatConn.connecting;
  bool running = false;
  bool liveConnected = false;
  String? error;

  int _seq = 0;
  bool _disposed = false;

  StreamSubscription<HubEvent>? _liveSub;
  Timer? _liveRetry;
  bool _liveWanted = false;
  int _liveAttempt = 0;
  Timer? _connRetry;
  int _connAttempt = 0;
  final Map<String, ChatItem> _liveTurns = {};
  final Set<String> _seenTurns = {};

  bool get empty => items.isEmpty;

  @override
  void dispose() {
    _disposed = true;
    _liveWanted = false;
    _liveRetry?.cancel();
    _liveSub?.cancel();
    _connRetry?.cancel();
    super.dispose();
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  // ------------------------------------------------------------------ 打开

  Future<void> open() async {
    stopLive();
    _connRetry?.cancel();
    _connRetry = null;
    conn = ChatConn.connecting;
    error = null;
    _notify();
    try {
      final history = await client.history(sessionId);
      if (_disposed) return;
      // 服务端回放 + 本地已有的不重复:历史只补到前面去。
      if (items.isEmpty) {
        items.addAll(history.map((m) => ChatItem(
              id: m.id,
              role: m.role,
              parts: List<MsgPart>.of(m.parts),
              time: m.time,
            )));
      }
      conn = ChatConn.connected;
      _connAttempt = 0;
      startLive();
    } catch (e) {
      conn = ChatConn.offline;
      error = e.toString();
      _scheduleReconnect();
    }
    _notify();
  }

  /// 完整版启动 AstrBot 要几十秒；失败后自己退避重试，最多约两分钟。
  void _scheduleReconnect() {
    if (_disposed || _connRetry != null || _connAttempt >= 8) return;
    final seconds = const [2, 3, 5, 8, 10, 15, 20, 30][_connAttempt.clamp(0, 7)];
    _connAttempt++;
    _connRetry = Timer(Duration(seconds: seconds), () {
      _connRetry = null;
      if (!_disposed) open();
    });
  }

  Future<void> reconnect() => open();

  /// 重新拉一次历史。流式期间不动，避免把正在长的那条冲掉。
  Future<void> refreshHistory() async {
    if (running || _liveTurns.isNotEmpty) return;
    try {
      final history = await client.history(sessionId);
      if (_disposed || running || _liveTurns.isNotEmpty) return;
      items
        ..clear()
        ..addAll(history.map((m) => ChatItem(
              id: m.id,
              role: m.role,
              parts: List<MsgPart>.of(m.parts),
              time: m.time,
            )));
      _notify();
    } catch (_) {
      // 保留现有内容，下次重连再试。
    }
  }

  /// 只在 `--dart-define=DEMO=true` 的验收包里用:塞一段样板对话,
  /// 不连服务器也能看排版、间距、气泡和流式光标。
  void seedDemo() {
    final now = DateTime.now();
    items
      ..clear()
      ..addAll([
        ChatItem(id: 'd1', role: ChatRole.her, parts: [const TextPart('你回来啦。今天外面冷不冷？')], time: now.subtract(const Duration(minutes: 8))),
        ChatItem(id: 'd2', role: ChatRole.me, parts: [const TextPart('有点冷，手都冻僵了')], time: now.subtract(const Duration(minutes: 7))),
        ChatItem(id: 'd3', role: ChatRole.her, parts: [const TextPart('那把手给我。\n……嗯，暖一点了吗？')], time: now.subtract(const Duration(minutes: 6))),
        ChatItem(id: 'd4', role: ChatRole.me, parts: [const TextPart('嗯，好多了。你今天在做什么？')], time: now.subtract(const Duration(minutes: 5))),
        ChatItem(
          id: 'd5',
          role: ChatRole.her,
          parts: [const TextPart('在等你呀，顺便把尾巴上的水擦干净。今天下了好大的雨，窗台上全是水珠，一颗一颗往下滚，看得我有点困。')],
          time: now.subtract(const Duration(minutes: 4)),
        ),
      ]);
    conn = ChatConn.connected;
    _notify();
  }

  // ------------------------------------------------------------------ 实时订阅

  void startLive() {
    if (_disposed || !client.hubEnabled || _liveSub != null) return;
    _liveWanted = true;
    _liveSub = client.subscribe(sessionId).listen(
      _onHubEvent,
      onError: (Object failure) => _onLiveDown(failure),
      onDone: () => _onLiveDown(null),
      cancelOnError: true,
    );
    // SSE 的 HTTP 建连没有单独事件；先乐观显示已开启，断开时再回落。
    liveConnected = true;
    _notify();
  }

  void stopLive() {
    _liveWanted = false;
    _liveRetry?.cancel();
    _liveRetry = null;
    _liveSub?.cancel();
    _liveSub = null;
    if (liveConnected) {
      liveConnected = false;
      _notify();
    }
  }

  void _onLiveDown(Object? failure) {
    _liveSub?.cancel();
    _liveSub = null;
    if (liveConnected) {
      liveConnected = false;
      _notify();
    }
    if (_disposed || !_liveWanted || _liveRetry != null) return;
    final seconds = const [1, 2, 5, 10][_liveAttempt.clamp(0, 3)];
    _liveAttempt++;
    _liveRetry = Timer(Duration(seconds: seconds), () {
      _liveRetry = null;
      if (!_disposed && _liveWanted) startLive();
    });
  }

  void _onHubEvent(HubEvent event) {
    _liveAttempt = 0;
    if (!liveConnected) {
      liveConnected = true;
      refreshHistory();
    }
    switch (event.type) {
      case HubEventType.userMessage:
        if (event.clientId == client.clientId) return;
        final turn = event.turnId;
        if (turn != null && !_seenTurns.add(turn)) return;
        if (event.parts.isEmpty) return;
        items.add(ChatItem(
          id: 'r${turn ?? DateTime.now().microsecondsSinceEpoch}',
          role: ChatRole.me,
          parts: List<MsgPart>.of(event.parts),
          time: DateTime.now(),
        ));
        if (_seenTurns.length > 400) _seenTurns.clear();
        _notify();
        break;

      case HubEventType.botEvent:
        final data = event.event;
        if (data == null) return;
        final turn = event.turnId ?? 'live-${DateTime.now().microsecondsSinceEpoch}';
        final item = _liveTurns.putIfAbsent(turn, () {
          final created = ChatItem(
            id: 'a$turn',
            role: ChatRole.her,
            parts: <MsgPart>[],
            time: DateTime.now(),
            streaming: true,
          );
          items.add(created);
          return created;
        });
        _applyHubEvent(item, data);
        _notify();
        break;

      case HubEventType.complete:
        _finishLiveTurn(event.turnId);
        break;

      case HubEventType.mirror:
        final text = event.mirrorText?.trim() ?? '';
        if (text.isEmpty) return;
        final ts = event.mirrorTime;
        final fromHer = event.mirrorRole == 'bot';
        items.add(ChatItem(
          id: 'tg${ts ?? DateTime.now().microsecondsSinceEpoch}',
          role: fromHer ? ChatRole.her : ChatRole.me,
          parts: [TextPart(text)],
          time: ts != null ? DateTime.fromMillisecondsSinceEpoch(ts) : DateTime.now(),
        ));
        _notify();
        break;

      case HubEventType.error:
        error = event.error ?? '实时同步出错';
        _finishLiveTurn(event.turnId);
        _notify();
        break;
    }
  }

  void _applyHubEvent(ChatItem item, Map<String, dynamic> event) {
    final type = event['type']?.toString() ?? '';
    switch (type) {
      case 'plain':
        final chain = event['chain_type']?.toString() ?? '';
        if (chain == 'reasoning' || chain.startsWith('tool')) return;
        final text = event['data']?.toString();
        if (text == null || text.isEmpty) return;
        if (event['streaming'] == true) {
          item.appendText(text);
        } else {
          item.replaceText(text);
        }
        break;
      case 'attachment_saved':
        final data = event['data'];
        if (data is Map) {
          final id = data['id']?.toString() ?? '';
          final kind = data['type']?.toString() ?? '';
          if (id.isNotEmpty) {
            item.addMedia(MediaPart(
              kind: switch (kind) {
                'image' => MediaKind.image,
                'record' => MediaKind.record,
                _ => MediaKind.file,
              },
              attachmentId: id,
            ));
          }
        }
        break;
      case 'error':
        error = event['data']?.toString() ?? '出错了';
        break;
      case 'complete':
      case 'end':
        item.streaming = false;
        break;
    }
  }

  void _finishLiveTurn(String? turnId) {
    if (turnId == null) {
      for (final item in _liveTurns.values.toList()) {
        item.streaming = false;
      }
      _liveTurns.clear();
      _notify();
      refreshHistory();
      return;
    }
    final item = _liveTurns.remove(turnId);
    if (item == null) return;
    item.streaming = false;
    if (!item.hasText && !item.hasMedia) items.remove(item);
    _notify();
    // 实时流可能在用户消息广播之后才建连；这一轮结束后拉一次历史补齐。
    refreshHistory();
  }

  // ------------------------------------------------------------------ 发送

  /// 发一条由若干片段组成的消息。返回时这一轮已经结束(或被 [stop] 中断)。
  Future<void> sendMessage(List<MsgPart> parts) async {
    if (parts.isEmpty || running) return;

    error = null;
    items.add(
      ChatItem(
        id: 'u${_seq++}',
        role: ChatRole.me,
        parts: List<MsgPart>.of(parts),
        time: DateTime.now(),
      ),
    );
    final reply = ChatItem(
      id: 'a${_seq++}',
      role: ChatRole.her,
      parts: <MsgPart>[],
      time: DateTime.now(),
      streaming: true,
    );
    items.add(reply);
    running = true;
    _notify();

    try {
      await for (final ev in client.sendParts(parts, sessionId: sessionId)) {
        if (ev.error != null) {
          error = ev.error;
          break;
        }
        if (ev.media != null) {
          reply.addMedia(ev.media!);
          _notify();
        }
        if (ev.text != null) {
          // streaming=true 是增量,false 是整段(见 AstrBot 4.29 的 SSE)。
          if (ev.streaming) {
            reply.appendText(ev.text!);
          } else {
            reply.replaceText(ev.text!);
          }
          if (conn != ChatConn.connected) conn = ChatConn.connected;
          _notify();
        }
        if (ev.done) break;
      }
    } catch (e) {
      error = e.toString();
      if (conn != ChatConn.offline) conn = ChatConn.offline;
    } finally {
      reply.streaming = false;
      // 一个字、一张图都没有就别留个空壳。
      if (!reply.hasText && !reply.hasMedia) items.remove(reply);
      running = false;
      _notify();
    }
  }

  /// 只有文字的快捷入口。
  Future<void> send(String text) => sendMessage([TextPart(text)]);

  /// 清空/删除:先让服务器把这段会话从库里删掉(消息、历史、附件文件),
  /// 再清本地列表与附件缓存。失败就把异常抛给界面,不要假装清掉了。
  Future<void> wipe() async {
    await client.deleteSession(sessionId);
    items.clear();
    _liveTurns.clear();
    _seenTurns.clear();
    Attachments.clear();
    error = null;
    running = false;
    _notify();
  }

  /// 「停止」:断流。已经收到的正文留着。
  void stop() {
    running = false;
    for (final it in items) {
      if (it.streaming) it.streaming = false;
    }
    _notify();
  }

  /// 演示/新会话:没有历史也要有个「已连接」的状态。
  void markConnected() {
    conn = ChatConn.connected;
    _notify();
  }

  void clearError() {
    if (error == null) return;
    error = null;
    _notify();
  }
}
