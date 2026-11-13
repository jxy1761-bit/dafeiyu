# 配置包（人设模板）

把 AstrBot 配置成「大肥鱼」的模板与导入脚本。

## 包含

- `settings/pack.json`：基础配置、插件开关与参数模板。
- `tools/import_dafeiyu.py`：按模板批量写入 AstrBot 配置。
- `tools/breeze/`：Breeze TTS 安装与音色制作脚本。
- `导入大肥鱼.bat` / `装Breeze语音.bat` / `做声音.bat`：Windows 快捷入口。
- `plugins/`：早期自研插件副本（clean / groupimage / memelog / petlink / resetkeep / signature / sing）。

## 不包含

- **人设模板**（请使用你自己的角色，导入时会自动跳过）。
- 表情包、Live2D 模型、声音参考音频、第三方语音模型。
- 任何账号、Cookie、密钥。

## 使用

1. 先在 AstrBot 后台配置好模型与平台。
2. 运行 `导入大肥鱼.bat`，按提示填写后台地址、账号、主人 QQ。
3. 需要语音时再运行 `装Breeze语音.bat` / `做声音.bat`。

导入脚本会覆盖同名配置项，修改前请先备份 `cmd_config.json`。

## 人设

仓库**不提供**人设模板。导入脚本检测不到模板时会自动跳过，请在 AstrBot 后台填写你自己的角色设定；请确保使用的形象、声音与素材拥有相应授权。
