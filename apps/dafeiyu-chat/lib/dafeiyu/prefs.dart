import 'dart:math';

import 'package:shared_preferences/shared_preferences.dart';

/// 本机配置：只存连接信息和一点界面偏好。
class Prefs {
  Prefs._(this._sp);

  final SharedPreferences _sp;

  static Prefs? _instance;

  static Future<Prefs> load() async {
    _instance ??= Prefs._(await SharedPreferences.getInstance());
    return _instance!;
  }

  static Prefs get i => _instance!;

  // ------------------------------------------------------------------ 连接

  String get baseUrl => _sp.getString('base_url') ?? '';
  set baseUrl(String v) => _sp.setString('base_url', v);

  String get username => _sp.getString('username') ?? '';
  set username(String v) => _sp.setString('username', v);

  String get password => _sp.getString('password') ?? '';
  set password(String v) => _sp.setString('password', v);

  /// 连成功过的地址,最多留 6 个:换网之后先挨个试,比扫网段快得多。
  List<String> get knownHosts => _sp.getStringList('known_hosts') ?? const [];

  Future<void> rememberHost(String url) async {
    final host = url.trim().replaceAll(RegExp(r'/+$'), '');
    if (host.isEmpty) return;
    final list = [...knownHosts]..remove(host);
    list.insert(0, host);
    if (list.length > 6) list.removeRange(6, list.length);
    await _sp.setStringList('known_hosts', list);
  }

  /// 允许自动识别服务器(扫局域网)。关掉就只用手填的地址。
  bool get autoDiscover => _sp.getBool('auto_discover') ?? true;
  set autoDiscover(bool v) => _sp.setBool('auto_discover', v);

  /// 实时互推:默认开。Hub 地址留空时按服务器地址自动推导到 6186。
  bool get realtime => _sp.getBool('realtime') ?? true;
  set realtime(bool v) => _sp.setBool('realtime', v);

  String get hubUrl {
    if (!realtime) return '';
    final saved = _sp.getString('hub_url')?.trim() ?? '';
    if (saved.isNotEmpty) return saved;
    final uri = Uri.tryParse(baseUrl.trim());
    if (uri == null || uri.host.isEmpty) return '';
    return '${uri.scheme}://${uri.host}:6186';
  }

  set hubUrl(String v) => _sp.setString('hub_url', v.trim());

  /// 本机安装的稳定标识,Hub 用它排除“自己发出去的消息”的广播。
  String get clientId {
    final saved = _sp.getString('client_id') ?? '';
    if (saved.isNotEmpty) return saved;
    final rand = Random();
    final id = 'c-${DateTime.now().millisecondsSinceEpoch.toRadixString(36)}'
        '-${rand.nextInt(1 << 32).toRadixString(36)}';
    _sp.setString('client_id', id);
    return id;
  }

  // ------------------------------------------------------------------ 会话

  /// 默认会话:所有设备共用同一个上下文。
  static const sharedSession = 'shared';

  /// 是否按设备隔离会话。**默认关**:关着的时候所有设备都进 [sharedSession],
  /// 谁说什么彼此都看得见,像同一个群里只有你们俩。
  bool get isolateSession => _sp.getBool('isolate_session') ?? false;
  set isolateSession(bool v) => _sp.setBool('isolate_session', v);

  /// 隔离模式下本机用的会话标识。第一次读的时候生成一个,之后固定不变,
  /// 免得每次重启都像开了新对话。
  String get deviceSessionId {
    final saved = _sp.getString('device_session') ?? '';
    if (saved.isNotEmpty) return saved;
    final rand = Random();
    final id = 'device-${rand.nextInt(9000) + 1000}';
    _sp.setString('device_session', id);
    return id;
  }

  set deviceSessionId(String v) => _sp.setString('device_session', v.trim());

  /// 真正发给 AstrBot 的会话 id。默认就是那个共享会话。
  String get sessionId => isolateSession ? deviceSessionId : sharedSession;

  // ------------------------------------------------------------------ 其它

  /// 角色区占屏高比例
  double get stageRatio => _sp.getDouble('stage_ratio') ?? 0.34;
  set stageRatio(double v) => _sp.setDouble('stage_ratio', v);

  /// 省电模式：暂停动态表情/动图，减少持续渲染。
  bool get powerSaver => _sp.getBool('power_saver') ?? true;
  set powerSaver(bool v) => _sp.setBool('power_saver', v);

  /// 界面帧率（60 / 120）。仅屏幕支持时生效。
  int get frameRate => _sp.getInt('frame_rate') ?? 60;
  set frameRate(int v) => _sp.setInt('frame_rate', v);

  bool get configured => username.isNotEmpty && password.isNotEmpty;
}
