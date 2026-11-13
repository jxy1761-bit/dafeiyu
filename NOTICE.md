# NOTICE · 第三方软件与致谢

本仓库包含或参考了以下开源项目。感谢所有作者与贡献者。

---

## 1. AstrBot for Android

- 项目：AstrBot for Android（AstrBot 运行管理器）
- 作者：JunWan666
- 仓库：https://github.com/JunWan666/astrbot-for-android
- 许可证：MIT

```
MIT License

Copyright (c) 2026 JunWan666

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

`apps/dafeiyu-full/android/manager` 基于该项目改造，主要修改：包名/品牌、聊天入口整合、性能设置、运行时组件裁剪与备份恢复衔接等。

---

## 2. dsh-Go（DeepSeek Harness）

- 项目：dsh-Go — DeepSeek Harness 装进口袋
- 作者：CikeSeven（GitHub: ccch1mneyyy）
- 仓库：https://github.com/ccch1mneyyy/dsh-Go
- 许可证：AGPL-3.0

本项目的 App UI 框架与设计体系（配色、圆角、玻璃质感、动效 token、排版、聊天页布局与交互范式）参考/改编自 dsh-Go。由于 dsh-Go 采用 AGPL-3.0，本仓库中由其衍生的 UI 代码同样按 AGPL-3.0 提供。特别感谢原作者及贡献者。

## 3. PiPilot

- 项目：PiPilot
- 作者：CikeSeven（GitHub: ccch1mneyyy）
- 仓库：https://github.com/ccch1mneyyy/pi_pilot
- 许可证：MIT

```
MIT License

Copyright (c) 2026 CikeSeven

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

涉及文件主要位于 `apps/*/lib/ui/theme/` 与 `apps/*/lib/ui/common/`，文件头保留了衍生说明。PiPilot 是与 dsh-Go 同作者的独立 MIT 项目，二者许可分别遵循。

---

## 4. AstrBot

- 项目：AstrBot
- 仓库：https://github.com/AstrBotDevs/AstrBot
- 许可证：AGPL-3.0
- 说明：本项目的服务端运行时与插件 API 依赖 AstrBot；本仓库不包含 AstrBot 本体。

## 5. LivingMemory

- 项目：astrbot_plugin_livingmemory
- 上游：https://github.com/lxfight/astrbot_plugin_livingmemory
- 许可证：AGPL-3.0（`plugins/astrbot_plugin_livingmemory/LICENSE`）
- 说明：本仓库中的版本包含大量本地改造（证据、情绪、时间线、冲突、关系状态、评估集），修改部分同样按 AGPL-3.0 提供。

## 6. 字体

- **JetBrains Mono**：SIL Open Font License 1.1，版权归 JetBrains 所有。
- **DshIcons**：随 dsh-Go 设计体系引入的图标字体，版权归原作者；如权利人认为不适合随本仓库分发，请联系删除，我们将替换为通用图标。

## 7. 未随仓库分发的第三方插件

以下插件由社区作者维护，本仓库只通过链接引用，不分发其代码，请从各自仓库获取并遵循其许可：

| 插件 | 作者 | 许可 |
|---|---|---|
| astrbot_plugin_group_chat_plus | Him666233 | AGPL-3.0 |
| astrbot_plugin_get_px | Sham1k0 | MIT |
| meme_manager | anka | MIT |
| astrbot_plugin_qzone_ultra | 雪碧bir | MIT |
| astrbot_plugin_parser | Zhalslar | MIT |
| astrbot_plugin_qqprofile | Zhalslar | AGPL-3.0 |
| astrbot_plugin_cost_control | Leafiber | 见上游 |
| astrbot_plugin_mihome | 见上游 | 见上游 |
| astrbot_plugin_stealer | nagatoquin33 | AGPL-3.0 |
| astrbot_plugin_temp_block | lingyun | MIT |
| astrbot_plugin_baidu_curl | jichie | MIT |
| astrbot_plugin_anima_master | YayiMiko | MIT |
| astrbot_plugin_qq_tools | YUMU1658 | 见上游 |
| astrbot_plugin_polite_silence | kitsuneimomo | 见上游 |

## 8. Flutter 与依赖

Flutter / Dart 采用 BSD-3-Clause；各 pub 依赖遵循 `pubspec.lock` 中对应包的许可证。

## 9. PRoot 运行时二进制

`apps/dafeiyu-full/android/manager/src/main/jniLibs/` 中的 `libproot*.so`、`libtalloc.so`、`libandroid-shmem.so` 等来自 AstrBot for Android / Termux 生态：

- PRoot：GPL-2.0，<https://github.com/proot-me/proot>
- talloc：LGPL-3.0（Samba 项目）
- android-shmem：Termux 项目，<https://github.com/termux/libandroid-shmem>

对应源代码可从上述上游获取。若你需要重新分发，请同时遵守 GPL/LGPL 的源码提供义务。

