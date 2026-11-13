# 大肥鱼·联网（astrbot_plugin_dafeiyu_web）

给她两个工具：`web_search`（上网搜一下）和 `web_page`（打开网页读正文）。

## 为什么自己做

AstrBot 自带的 `web_search` 依赖 tavily / bocha / exa / brave 之类的 key，`provider_settings.web_search`
默认还是关的，没 key 就直接不挂工具——所以她会一直「上不了网」。

本插件自带两条腿，不写进主配置：

1. **TinyFish**（主用）：`api.search.tinyfish.ai` 搜索、`api.fetch.tinyfish.ai` 抽正文（markdown）。
   结果干净、能整页读，一次固定 10 条。
2. **Bing 的 RSS 口**（兜底）：`cn.bing.com/search?format=rss`，不用注册、没有额度，TinyFish 挂了/没 key 时自动用。

## 密钥（重要）

key **不写进 AstrBot 配置、不进仓库**，只从本机文件读，顺序：

1. 环境变量 `TINYFISH_API_KEY`
2. `~/.dafeiyu/tinyfish.key`（Linux / Termux / proot 下是 `/root/.dafeiyu/tinyfish.key`）
3. `%LOCALAPPDATA%\dafeiyu\tinyfish.key`（Windows）

一行一个 key，别带空格。日志里只报「key 已就位 / 没读到」，从不打印 key 本身。

## 工具

| 工具 | 作用 | 典型调用 |
| --- | --- | --- |
| `web_search` | 搜 5 条结果（标题 + 链接 + 220 字摘要） | 「杭州今天天气」「哪吒2 票房」 |
| `web_page` | 打开一个 http(s) 链接，读正文前 2600 字 | 对方甩来一个链接让我看 |

## 日志

```
[dafeiyu-web] TinyFish key 已就位，搜索走 TinyFish（Bing 兜底）
[dafeiyu-web] 没读到 TinyFish key，只用 Bing 的 RSS 结果
[dafeiyu-web] 搜索 '杭州今天天气' → tinyfish 命中 5 条
[dafeiyu-web] 读取 https://… 正文 1652 字（TinyFish）
[dafeiyu-web] 搜索异常: ...
```

## 排错

| 现象 | 检查 |
| --- | --- |
| 日志没有 `[dafeiyu-web]` 这两行之一 | 插件没加载（`GET /api/v1/plugins` 看 `activated`） |
| 提示「未找到指定的工具: web_search」 | 用户人设的工具白名单被写成 `[]`；改成 `null` 即恢复 |
| 一直只有 Bing 结果 | key 文件路径不对或内容带空格/换行 |
| `web_page` 说「这个网页基本没有可读的正文」 | 图片站、需要登录、纯前端渲染（读不到 SSR 内容） |

## 链接阅读（B站 / 抖音 / 贴吧 / 小黑盒）

对方发来链接时，插件会在 `on_llm_request` 里自动打开一次，把标题、正文、视频里说的话作为
**临时片段**交给模型（`mark_as_temp`，不写进聊天历史，不会反复占上下文）。

- B站 / 抖音 / 贴吧 / 小黑盒：统一交给 [ParseHub](https://github.com/z-mio/ParseHub)（MIT）解析。
  它已经处理了各站点的签名、移动端兜底和正文抽取；抖音/贴吧/小黑盒都有现成 parser。
- 视频：用 ParseHub 拿到的视频直链，本地 ffmpeg 只抽前 3 分钟音频，再用本机 sherpa-onnx
  转成文字；不下载完整视频，也不调用云端 ASR。
- 封面：视频/图文的 `thumb_url` 作为临时图片一起给模型看；不逐帧分析画面，画面细节仍建议发视频文件。
- 普通网页：ParseHub 不支持时退回 TinyFish / 直接抓 HTML。

依赖装在 AstrBot 的 Python 环境里：`pip install parsehub`（或按本目录 `requirements.txt`）。
没装 ParseHub 时插件仍能启动，只是这四个站点会退回普通网页抓取。



## 复用 Edge 登录 Cookie（可选）

**不能直接读 Edge 的 Cookies 数据库**：Edge 127+ 的 Cookie 用 App-Bound Encryption 保护，
第三方离线解密会报 `Failed to decrypt with DPAPI`（yt-dlp 也一样）。GitHub 上稳定的做法是
从浏览器里导出一次：

1. Edge 安装开源的 **Get cookies.txt LOCALLY**（MIT）：
   <https://github.com/kairi003/Get-cookies.txt-LOCALLY>
   或 **Cookie-Editor**（GPL-3.0）：<https://github.com/Moustachauve/cookie-editor>
2. 在 Edge 里登录 B站 / 抖音 / 贴吧 / 小黑盒。
3. 点扩展 → Export → 选 Netscape 格式，保存为：
   `<project>/private/edge-cookies.txt`
   也支持 Cookie-Editor 导出的 JSON，放到 `private\parsehub_cookies.json`。
4. 插件会按域名挑 Cookie 传给 ParseHub；文件更新后链接缓存会自动失效。

Cookie 文件等同于登录凭证：只放本机 `private/`，不要截图、不要上传、不要提交仓库。
公开内容不需要 Cookie；只有登录可见、风控或频控的页面才用得上。Cookie 过期后重新导出即可。


### 更省事：控制台「链接登录」专用 Edge

控制台新增「链接登录」卡片：点「打开登录窗口」会用独立的
`private/edge-bot-profile` 启动 Edge，并开本机调试端口 9223，不影响你日常 Edge。
在里面登录一次 B站/抖音/贴吧/小黑盒后，插件通过 CDP 的 `Storage.getCookies`
实时读 Cookie（浏览器自己解密），不用再手动导出。窗口关掉后登录态还在这个专用配置里，
下次再打开即可。调试端口只绑 127.0.0.1；这个专用配置目录不要删。

## 说明

- 这是聊天用的轻量联网：不扒大站、不并发抓几十页，避免塞爆上下文和触发风控。
- 手机端与电脑端跑同一份代码，key 各自放本机（同步时不会带 key）。