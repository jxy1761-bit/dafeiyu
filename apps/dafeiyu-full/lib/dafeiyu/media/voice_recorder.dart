import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';
import 'package:record/record.dart';

import '../../ui/common/pressable.dart';
import '../../ui/theme/theme.dart';

/// 录音结果。
class RecordedVoice {
  const RecordedVoice(this.bytes, this.seconds);
  final Uint8List bytes;
  final double seconds;
}

/// 录音面板:点一下开始,再点一下结束,然后发送或重录。
///
/// 用的是 `record` 插件录 16k 单声道 PCM WAV —— AstrBot 那边 STT 要的就是
/// 这个采样率,发原声也能直接转文字。
Future<RecordedVoice?> recordVoiceSheet(BuildContext context) {
  return showModalBottomSheet<RecordedVoice>(
    context: context,
    backgroundColor: Colors.transparent,
    isDismissible: false,
    barrierColor: Colors.black.withValues(alpha: 0.45),
    builder: (context) => const _VoiceSheet(),
  );
}

class _VoiceSheet extends StatefulWidget {
  const _VoiceSheet();

  @override
  State<_VoiceSheet> createState() => _VoiceSheetState();
}

class _VoiceSheetState extends State<_VoiceSheet> {
  final _rec = AudioRecorder();
  Timer? _tick;
  String? _path;
  bool _recording = false;
  double _seconds = 0;
  bool _denied = false;

  @override
  void dispose() {
    _tick?.cancel();
    _rec.dispose();
    super.dispose();
  }

  Future<void> _start() async {
    if (!await _rec.hasPermission()) {
      setState(() => _denied = true);
      return;
    }
    final dir = await getTemporaryDirectory();
    final path = '${dir.path}/voice_${DateTime.now().millisecondsSinceEpoch}.wav';
    await _rec.start(
      const RecordConfig(encoder: AudioEncoder.wav, sampleRate: 16000, numChannels: 1),
      path: path,
    );
    setState(() {
      _recording = true;
      _seconds = 0;
      _path = path;
    });
    _tick = Timer.periodic(const Duration(milliseconds: 100), (_) {
      if (mounted) setState(() => _seconds += 0.1);
    });
  }

  Future<void> _stop() async {
    _tick?.cancel();
    final path = await _rec.stop();
    if (!mounted) return;
    setState(() {
      _recording = false;
      _path = path ?? _path;
    });
  }

  Future<void> _send() async {
    final p = _path;
    if (p == null) return;
    final file = File(p);
    if (!await file.exists()) return;
    final bytes = await file.readAsBytes();
    if (!mounted) return;
    Navigator.of(context).pop(RecordedVoice(bytes, _seconds));
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final dsh = DshColors.of(context);
    final done = !_recording && _path != null;

    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
        child: GlassSurface(
          tier: GlassTier.sheet,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 18, 20, 16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(_recording ? '正在录音' : (done ? '录好了' : '发语音'),
                    style: theme.textTheme.titleSmall),
                const SizedBox(height: 6),
                Text(
                  _denied ? '没有麦克风权限，去系统设置里打开' : _fmt(_seconds),
                  style: AppType.display(size: 30, color: _recording ? dsh.danger : null),
                ),
                const SizedBox(height: 18),
                Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    _Circle(
                      icon: _recording ? AppIcons.stopGenerating : AppIcons.voice,
                      label: _recording ? '停止' : (done ? '重录' : '录音'),
                      danger: _recording,
                      onTap: () async {
                        if (_recording) {
                          await _stop();
                        } else {
                          setState(() => _path = null);
                          await _start();
                        }
                      },
                    ),
                    if (done) ...[
                      const SizedBox(width: 28),
                      _Circle(
                        icon: AppIcons.send,
                        label: '发送',
                        primary: true,
                        onTap: _send,
                      ),
                    ],
                  ],
                ),
                const SizedBox(height: 10),
                Pressable(
                  onTap: () async {
                    if (_recording) await _stop();
                    if (context.mounted) Navigator.of(context).pop();
                  },
                  child: Padding(
                    padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 16),
                    child: Text('取消',
                        style: theme.textTheme.labelLarge?.copyWith(color: dsh.textTertiary)),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  String _fmt(double s) {
    final total = s.round();
    return '${(total ~/ 60).toString().padLeft(1, '0')}:${(total % 60).toString().padLeft(2, '0')}';
  }
}

class _Circle extends StatelessWidget {
  const _Circle({
    required this.icon,
    required this.label,
    required this.onTap,
    this.primary = false,
    this.danger = false,
  });

  final IconData icon;
  final String label;
  final VoidCallback onTap;
  final bool primary;
  final bool danger;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    final dsh = DshColors.of(context);
    final bg = primary
        ? colors.primary
        : danger
            ? dsh.danger
            : colors.onSurface.withValues(alpha: 0.08);
    final fg = primary ? colors.onPrimary : (danger ? dsh.onDanger : colors.onSurface);
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Pressable(
          onTap: onTap,
          child: Container(
            width: 56,
            height: 56,
            decoration: BoxDecoration(color: bg, shape: BoxShape.circle),
            child: Icon(icon, size: 24, color: fg),
          ),
        ),
        const SizedBox(height: 6),
        Text(label, style: Theme.of(context).textTheme.labelSmall?.copyWith(color: dsh.textTertiary)),
      ],
    );
  }
}
