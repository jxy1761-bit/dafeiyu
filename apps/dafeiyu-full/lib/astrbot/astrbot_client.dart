import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;

import 'models.dart';

/// AstrBot dashboard 的瘦客户端。
///
/// 四件事:换 JWT、发消息(文字 + 媒体)接 SSE 流、按会话 id 拉历史、
/// 传/取附件。
///
/// 完整版/基础版 v0.2 起增加 Hub:
/// - 发送优先走 `hubUrl/hub/send`，Hub 会把消息广播给同会话的其他设备；
/// - `subscribe()` 订阅 Hub 的实时事件；
/// - Hub 不可达时自动退回直连 AstrBot 的 SSE。
class AstrBotClient {
  AstrBotClient({
    required this.baseUrl,
    required this.username,
    required this.password,
    String? hubUrl,
    String? clientId,
    http.Client? client,
  })  : hubUrl = (hubUrl == null || hubUrl.trim().isEmpty) ? null : hubUrl.trim(),
        clientId = clientId ?? 'client-${DateTime.now().millisecondsSinceEpoch}',
        _http = client ?? http.Client();

  final String baseUrl;
  final String username;
  final String password;
  final String? hubUrl;
  final String clientId;
  final http.Client _http;

  String? _token;
  DateTime? _tokenAt;

  static const _json = 'application/json; charset=utf-8';

  String get _root => baseUrl.trim().replaceAll(RegExp(r'/+$'), '');

  String get _hubRoot => (hubUrl ?? '').trim().replaceAll(RegExp(r'/+$'), '');

  bool get hubEnabled => _hubRoot.isNotEmpty;

  // ---------------------------------------------------------------- 登录

  Future<String> login({bool force = false}) async {
    final now = DateTime.now();
    final cached = _token;
    if (!force && cached != null && _tokenAt != null &&
        now.difference(_tokenAt!).inMinutes < 15) {
      return cached;
    }
    final res = await _http
        .post(
          Uri.parse('$_root/api/auth/login'),
          headers: {'Content-Type': _json},
          body: jsonEncode({'username': username, 'password': password}),
        )
        .timeout(const Duration(seconds: 12));

    final body = jsonDecode(utf8.decode(res.bodyBytes)) as Map<String, dynamic>;
    if (res.statusCode != 200 || body['status'] != 'ok') {
      throw AstrBotException(body['message']?.toString() ?? '账号或密码错误');
    }
    final data = body['data'] as Map<String, dynamic>?;
    final token = data?['token']?.toString();
    if (token == null || token.isEmpty) throw AstrBotException('登录返回里没有 token');
    _token = token;
    _tokenAt = now;
    return token;
  }

  void invalidateToken() {
    _token = null;
    _tokenAt = null;
  }

  /// 探一下能不能连上、账号对不对
  Future<String> ping() async {
    await login(force: true);
    return '连接成功';
  }

  // ---------------------------------------------------------------- 历史

  Future<List<ChatMessage>> history(String sessionId, {int limit = 200}) async {
    for (var attempt = 0; attempt < 2; attempt++) {
      try {
        final token = await login(force: attempt > 0);
        final uri = Uri.parse(
          '$_root/api/v1/chat/sessions/${Uri.encodeComponent(sessionId)}'
          '?page=1&page_size=$limit',
        );
        final res = await _http
            .get(uri, headers: {'Authorization': 'Bearer $token'})
            .timeout(const Duration(seconds: 20));
        if (res.statusCode == 401 || res.statusCode == 403) continue;
        if (res.statusCode != 200) throw AstrBotException('HTTP ${res.statusCode}');
        return _parseHistory(jsonDecode(utf8.decode(res.bodyBytes)));
      } catch (e) {
        if (attempt == 1) rethrow;
      }
    }
    return const [];
  }

  List<ChatMessage> _parseHistory(dynamic root) {
    final data = (root as Map<String, dynamic>)['data'];
    final list = (data is Map<String, dynamic>)
        ? (data['history'] ?? data['messages']) as List<dynamic>?
        : null;
    if (list == null) return const [];

    final out = <ChatMessage>[];
    for (var i = 0; i < list.length; i++) {
      final item = list[i];
      if (item is! Map<String, dynamic>) continue;
      dynamic content = item['content'];
      if (content is String) {
        try {
          content = jsonDecode(content);
        } catch (_) {
          continue;
        }
      }
      if (content is! Map<String, dynamic>) continue;

      final role = content['type'] == 'bot' ? ChatRole.her : ChatRole.me;
      final parts = content['message'];
      if (parts is! List) continue;

      final parsed = <MsgPart>[];
      final text = StringBuffer();
      for (final p in parts) {
        if (p is! Map<String, dynamic>) continue;
        switch (p['type']) {
          case 'plain':
            final t = p['text']?.toString() ?? '';
            if (t.isEmpty) continue;
            text.write(t);
            parsed.add(TextPart(t));
          case 'image':
          case 'record':
          case 'file':
          case 'video':
            final id = p['attachment_id']?.toString() ?? '';
            if (id.isEmpty) continue;
            parsed.add(
              MediaPart(
                kind: switch (p['type']) {
                  'image' => MediaKind.image,
                  'record' => MediaKind.record,
                  _ => MediaKind.file,
                },
                attachmentId: id,
                name: p['filename']?.toString(),
              ),
            );
        }
      }
      final joined = text.toString().trim();
      if (joined.isEmpty && parsed.isEmpty) continue;
      final id = (item['id'] ?? i).toString();
      out.add(
        ChatMessage(
          role: role,
          text: joined,
          parts: parsed,
          id: 'h$id',
          time: DateTime.now(),
        ),
      );
    }
    return out;
  }

  // ---------------------------------------------------------------- 附件

  /// 传一个文件,拿到 attachment_id。`type` 由服务端按 MIME 判断。
  Future<MediaPart> upload({
    required Uint8List bytes,
    required String filename,
    required MediaKind kind,
    String? mime,
    double? duration,
  }) async {
    final token = await login();
    final req = http.MultipartRequest('POST', Uri.parse('$_root/api/chat/post_file'))
      ..headers['Authorization'] = 'Bearer $token'
      ..files.add(
        http.MultipartFile.fromBytes(
          'file',
          bytes,
          filename: filename,
          contentType: mime == null ? null : _mediaType(mime),
        ),
      );
    final streamed = await _http.send(req).timeout(const Duration(seconds: 60));
    final res = await http.Response.fromStream(streamed);
    final body = jsonDecode(utf8.decode(res.bodyBytes));
    if (body is! Map || body['status'] != 'ok') {
      throw AstrBotException(body is Map ? (body['message']?.toString() ?? '上传失败') : '上传失败');
    }
    final data = body['data'];
    if (data is! Map) throw AstrBotException('上传返回异常');
    final id = data['attachment_id']?.toString() ?? '';
    if (id.isEmpty) throw AstrBotException('上传没有返回 attachment_id');
    final served = data['type']?.toString();
    return MediaPart(
      kind: switch (served) {
        'image' => MediaKind.image,
        'record' => MediaKind.record,
        _ => kind,
      },
      attachmentId: id,
      name: data['filename']?.toString() ?? filename,
      bytes: bytes.length,
      mime: mime,
      duration: duration,
    );
  }

  http.MediaType _mediaType(String mime) {
    final parts = mime.split('/');
    return http.MediaType(parts.first, parts.length > 1 ? parts[1] : 'octet-stream');
  }

  /// 取附件字节(图片显示、语音播放、文件下载都走它)。
  Future<Uint8List> attachmentBytes(String attachmentId) async {
    final token = await login();
    final uri = Uri.parse(
      '$_root/api/chat/get_attachment?attachment_id=${Uri.encodeComponent(attachmentId)}',
    );
    final res = await _http
        .get(uri, headers: {'Authorization': 'Bearer $token'})
        .timeout(const Duration(seconds: 60));
    if (res.statusCode == 401) {
      invalidateToken();
      throw AstrBotException('登录过期');
    }
    if (res.statusCode != 200) throw AstrBotException('附件 ${res.statusCode}');
    return res.bodyBytes;
  }

  /// 删掉一段会话:服务器那边会连消息、平台历史、附件文件一起清掉。
  Future<void> deleteSession(String sessionId) async {
    final token = await login();
    final uri = Uri.parse(
      '$_root/api/chat/delete_session?session_id=${Uri.encodeComponent(sessionId)}',
    );
    final res = await _http
        .get(uri, headers: {'Authorization': 'Bearer $token'})
        .timeout(const Duration(seconds: 30));
    final body = jsonDecode(utf8.decode(res.bodyBytes));
    if (res.statusCode != 200) {
      throw AstrBotException('删除失败：HTTP ${res.statusCode}');
    }
    if (body is Map && body['status'] != 'ok') {
      throw AstrBotException(body['message']?.toString() ?? '删除失败');
    }
  }

  /// 附件的直链(给 Image.network 之类;需要带 token,所以用得少)。
  String attachmentUrl(String attachmentId) =>
      '$_root/api/chat/get_attachment?attachment_id=${Uri.encodeComponent(attachmentId)}';

  // ---------------------------------------------------------------- 发送

  /// 发一条文字(兼容旧调用)。
  Stream<ChatEvent> send(String text, {required String sessionId}) =>
      sendParts([TextPart(text)], sessionId: sessionId);

  /// 发一条由若干片段组成的消息,把 SSE 事件按顺序吐出来。
  Stream<ChatEvent> sendParts(List<MsgPart> parts, {required String sessionId}) async* {
    if (parts.isEmpty) return;
    final token = await login();

    if (hubEnabled) {
      var emitted = false;
      try {
        await for (final event in _sendViaHub(parts, sessionId, token)) {
          emitted = true;
          yield event;
        }
        return;
      } on _HubUnavailable {
        if (emitted) rethrow;
      }
    }

    yield* _sendDirect(parts, sessionId, token);
  }

  Stream<ChatEvent> _sendViaHub(List<MsgPart> parts, String sessionId, String token) async* {
    final request = http.Request('POST', Uri.parse('$_hubRoot/hub/send'))
      ..headers.addAll({
        'Authorization': 'Bearer $token',
        'Content-Type': _json,
        'Accept': 'text/event-stream',
      })
      ..body = jsonEncode({
        'session_id': sessionId,
        'client_id': clientId,
        'message': parts.map((p) => p.toJson()).toList(),
      });

    http.StreamedResponse response;
    try {
      response = await _http.send(request).timeout(const Duration(seconds: 12));
    } catch (failure) {
      throw _HubUnavailable('$failure');
    }
    if (response.statusCode == 401) {
      invalidateToken();
      throw AstrBotException('登录过期，请重试');
    }
    if (response.statusCode == 409) {
      throw AstrBotException('上一条消息还在回复，请稍候');
    }
    if (response.statusCode == 404) {
      throw _HubUnavailable('hub not found');
    }
    if (response.statusCode != 200) {
      throw AstrBotException('Hub ${response.statusCode}');
    }
    yield* _parseSse(response.stream);
  }

  Stream<ChatEvent> _sendDirect(List<MsgPart> parts, String sessionId, String token) async* {
    final req = http.Request('POST', Uri.parse('$_root/api/chat/send'))
      ..headers.addAll({
        'Authorization': 'Bearer $token',
        'Content-Type': _json,
        'Accept': 'text/event-stream',
      })
      ..body = jsonEncode({
        'session_id': sessionId,
        'message': parts.map((p) => p.toJson()).toList(),
      });

    final resp = await _http.send(req);
    if (resp.statusCode == 401) {
      invalidateToken();
      throw AstrBotException('登录过期，请重试');
    }
    if (resp.statusCode != 200) {
      throw AstrBotException('服务端 ${resp.statusCode}');
    }
    yield* _parseSse(resp.stream);
  }

  Stream<ChatEvent> _parseSse(Stream<List<int>> stream) async* {
    final lines = stream.transform(utf8.decoder).transform(const LineSplitter());
    await for (final line in lines) {
      if (!line.startsWith('data:')) continue; // ": heartbeat" 之类直接跳过
      final payload = line.substring(5).trim();
      if (payload.isEmpty) continue;

      final Map<String, dynamic> obj;
      try {
        obj = jsonDecode(payload) as Map<String, dynamic>;
      } catch (_) {
        continue;
      }

      final type = obj['type']?.toString() ?? '';
      switch (type) {
        case 'plain':
          final chain = obj['chain_type']?.toString() ?? '';
          // 思考过程和工具往返不算正文
          if (chain == 'reasoning' || chain.startsWith('tool')) continue;
          final t = obj['data']?.toString();
          if (t == null || t.isEmpty) continue;
          yield ChatEvent(text: t, streaming: obj['streaming'] == true);
          break;
        case 'attachment_saved':
          // {type:'attachment_saved', data:{id, type}}
          final data = obj['data'];
          if (data is Map) {
            final id = data['id']?.toString() ?? '';
            final kind = data['type']?.toString() ?? '';
            if (id.isNotEmpty) {
              yield ChatEvent(
                media: MediaPart(
                  kind: switch (kind) {
                    'image' => MediaKind.image,
                    'record' => MediaKind.record,
                    _ => MediaKind.file,
                  },
                  attachmentId: id,
                ),
              );
            }
          }
          break;
        case 'error':
          yield ChatEvent(error: obj['data']?.toString() ?? '出错了');
          break;
        case 'complete':
        case 'end':
          yield const ChatEvent(done: true);
          return;
      }
    }
    yield const ChatEvent(done: true);
  }

  // ---------------------------------------------------------------- Hub 订阅

  /// 订阅 Hub 的同会话广播。调用方负责断线重连。
  Stream<HubEvent> subscribe(String sessionId) async* {
    if (!hubEnabled) return;
    final token = await login();
    final uri = Uri.parse('$_hubRoot/hub/subscribe').replace(queryParameters: {
      'session_id': sessionId,
      'client_id': clientId,
    });
    final request = http.Request('GET', uri)
      ..headers.addAll({
        'Authorization': 'Bearer $token',
        'Accept': 'text/event-stream',
        'Cache-Control': 'no-cache',
      });
    http.StreamedResponse response;
    try {
      response = await _http.send(request).timeout(const Duration(seconds: 12));
    } catch (failure) {
      throw _HubUnavailable('$failure');
    }
    if (response.statusCode == 401) {
      invalidateToken();
      throw AstrBotException('登录过期');
    }
    if (response.statusCode != 200) {
      throw _HubUnavailable('hub ${response.statusCode}');
    }

    final lines = response.stream.transform(utf8.decoder).transform(const LineSplitter());
    await for (final line in lines) {
      if (!line.startsWith('data:')) continue;
      final raw = line.substring(5).trim();
      if (raw.isEmpty) continue;
      final Map<String, dynamic> obj;
      try {
        obj = jsonDecode(raw) as Map<String, dynamic>;
      } catch (_) {
        continue;
      }
      final type = obj['type']?.toString() ?? '';
      switch (type) {
        case 'user_message':
          yield HubEvent(
            type: HubEventType.userMessage,
            clientId: obj['client_id']?.toString(),
            turnId: obj['turn_id']?.toString(),
            parts: parseParts(obj['message']),
          );
          break;
        case 'bot_event':
          final event = obj['event'];
          if (event is Map<String, dynamic>) {
            yield HubEvent(
              type: HubEventType.botEvent,
              turnId: obj['turn_id']?.toString(),
              event: event,
            );
          }
          break;
        case 'complete':
          yield HubEvent(type: HubEventType.complete, turnId: obj['turn_id']?.toString());
          break;
        case 'error':
          yield HubEvent(
            type: HubEventType.error,
            turnId: obj['turn_id']?.toString(),
            error: obj['data']?.toString() ?? '出错了',
          );
          break;
        case 'mirror':
          yield HubEvent(
            type: HubEventType.mirror,
            mirrorRole: obj['role']?.toString(),
            mirrorSender: obj['sender']?.toString(),
            mirrorText: obj['text']?.toString(),
            mirrorTime: (obj['ts'] as num?)?.toInt(),
          );
          break;
      }
    }
  }

  /// 把 AstrBot 的 message parts 转成本地片段。
  static List<MsgPart> parseParts(dynamic value) {
    final out = <MsgPart>[];
    if (value is! List) return out;
    for (final raw in value) {
      if (raw is! Map) continue;
      final part = raw.cast<String, dynamic>();
      switch (part['type']) {
        case 'plain':
          final text = part['text']?.toString() ?? '';
          if (text.isNotEmpty) out.add(TextPart(text));
          break;
        case 'image':
        case 'record':
        case 'file':
        case 'video':
          final id = part['attachment_id']?.toString() ?? '';
          if (id.isEmpty) continue;
          out.add(
            MediaPart(
              kind: switch (part['type']) {
                'image' => MediaKind.image,
                'record' => MediaKind.record,
                _ => MediaKind.file,
              },
              attachmentId: id,
              name: part['filename']?.toString(),
            ),
          );
          break;
      }
    }
    return out;
  }

  void close() => _http.close();
}

enum ChatRole { me, her }

class ChatMessage {
  ChatMessage({
    required this.role,
    required this.text,
    required this.id,
    required this.time,
    List<MsgPart>? parts,
  }) : parts = parts ?? (text.isEmpty ? [] : [TextPart(text)]);

  final ChatRole role;
  final String id;
  final DateTime time;
  String text;
  final List<MsgPart> parts;
}

class ChatEvent {
  const ChatEvent({this.text, this.streaming = false, this.done = false, this.error, this.media});

  final String? text;
  final bool streaming;
  final bool done;
  final String? error;

  /// 服务端刚落库的一张图 / 一段语音 / 一个文件。
  final MediaPart? media;
}

enum HubEventType { userMessage, botEvent, complete, error, mirror }

/// Hub 广播出来的一条事件。
class HubEvent {
  const HubEvent({
    required this.type,
    this.clientId,
    this.turnId,
    this.parts = const [],
    this.event,
    this.error,
    this.mirrorRole,
    this.mirrorSender,
    this.mirrorText,
    this.mirrorTime,
  });

  final HubEventType type;
  final String? clientId;
  final String? turnId;
  final List<MsgPart> parts;
  final Map<String, dynamic>? event;
  final String? error;
  final String? mirrorRole;
  final String? mirrorSender;
  final String? mirrorText;
  final int? mirrorTime;
}

class AstrBotException implements Exception {
  AstrBotException(this.message);
  final String message;

  @override
  String toString() => message;
}

class _HubUnavailable implements Exception {
  _HubUnavailable(this.message);
  final String message;

  @override
  String toString() => 'Hub 不可用：$message';
}
