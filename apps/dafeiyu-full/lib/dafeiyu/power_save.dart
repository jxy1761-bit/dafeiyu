import 'package:flutter/foundation.dart';

import 'prefs.dart';

/// 省电模式：冻结聊天里的动态表情/动图，减少持续重绘。
class PowerSave {
  static final ValueNotifier<bool> enabled = ValueNotifier<bool>(false);

  static void load() {
    enabled.value = Prefs.i.powerSaver;
  }

  static void set(bool value) {
    Prefs.i.powerSaver = value;
    enabled.value = value;
  }
}
