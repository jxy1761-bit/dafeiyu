import 'dart:async';
import 'dart:io';

/// 局域网里找 AstrBot。
///
/// 手机换 WiFi / 热点 / 流量之后 IP 会变,地址写死就废了。这里做两件事:
///
/// 1. **先试记得的**:上次连通的地址、历史连过的地址,一次 TCP 探测就知道;
/// 2. **再扫同网段**:拿本机的 IPv4 推出 /24(Dart 的 NetworkInterface 不给
///    掩码,手机热点和家用 WiFi 基本都是 /24),254 个地址分批并发探 6185
///    端口 —— 端口开着基本就是 AstrBot,再补一次 HTTP 确认。
///
/// 全程不依赖任何插件:只有 `dart:io` 的 Socket。
class Discovery {
  const Discovery._();

  /// AstrBot 面板默认端口。
  static const defaultPort = 6185;

  /// 一次扫描的结果:按“像不像”排序的 base url 列表。
  static Future<List<String>> scan({
    int port = defaultPort,
    Duration timeout = const Duration(milliseconds: 240),
    int batch = 64,
    void Function(double progress)? onProgress,
    Set<String> skip = const {},
  }) async {
    final locals = await _localSubnets();
    if (locals.isEmpty) return const [];

    final candidates = <String>[];
    for (final prefix in locals) {
      for (var i = 1; i < 255; i++) {
        final ip = '$prefix.$i';
        if (skip.contains(ip)) continue;
        candidates.add(ip);
      }
    }
    if (candidates.isEmpty) return const [];

    final hits = <String>[];
    var done = 0;
    for (var start = 0; start < candidates.length; start += batch) {
      final slice = candidates.skip(start).take(batch).toList();
      final results = await Future.wait(
        slice.map((ip) => _tcpOpen(ip, port, timeout)),
      );
      for (var i = 0; i < slice.length; i++) {
        if (results[i]) hits.add('http://${slice[i]}:$port');
      }
      done += slice.length;
      onProgress?.call(done / candidates.length);
    }
    return hits;
  }

  /// 只探一个地址:能不能连上 port。
  static Future<bool> _tcpOpen(String host, int port, Duration timeout) async {
    Socket? socket;
    try {
      socket = await Socket.connect(host, port, timeout: timeout);
      return true;
    } catch (_) {
      return false;
    } finally {
      socket?.destroy();
    }
  }

  /// 本机的 IPv4 网段前缀(a.b.c)。接流量时没有私有地址,返回空。
  static Future<Set<String>> _localSubnets() async {
    final out = <String>{};
    try {
      final interfaces = await NetworkInterface.list(
        type: InternetAddressType.IPv4,
        includeLoopback: false,
        includeLinkLocal: false,
      );
      for (final ni in interfaces) {
        for (final addr in ni.addresses) {
          final ip = addr.address;
          if (addr.isLoopback) continue;
          final parts = ip.split('.');
          if (parts.length != 4) continue;
          // 私有网段才扫:10.x / 172.16-31.x / 192.168.x
          final a = int.tryParse(parts[0]) ?? 0;
          final b = int.tryParse(parts[1]) ?? 0;
          final priv = a == 10 || (a == 172 && b >= 16 && b <= 31) || (a == 192 && b == 168);
          if (!priv) continue;
          out.add('${parts[0]}.${parts[1]}.${parts[2]}');
        }
      }
    } catch (_) {
      // 拿不到网卡就当扫不了
    }
    return out;
  }

  /// 这个地址上有没有 AstrBot 面板(HTTP 200/401/405 都算活着)。
  static Future<bool> alive(String baseUrl, {Duration timeout = const Duration(seconds: 2)}) async {
    final url = baseUrl.trim().replaceAll(RegExp(r'/+$'), '');
    if (url.isEmpty) return false;
    final client = HttpClient()..connectionTimeout = timeout;
    try {
      final req = await client.getUrl(Uri.parse(url)).timeout(timeout);
      final res = await req.close().timeout(timeout);
      await res.drain<void>();
      return res.statusCode < 500;
    } catch (_) {
      return false;
    } finally {
      client.close(force: true);
    }
  }
}
