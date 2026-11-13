# 更新日志

## v0.1.0 — 2026-11-03
- 新增 `web_search` / `web_page` 两个工具。
- 搜索主用 TinyFish（`api.search.tinyfish.ai`），失败自动退回 Bing 的 RSS 口。
- `web_page` 先用 TinyFish 抽 markdown，抽不出再直接抓 HTML 清洗。
- 密钥只从环境变量 / 本机密钥文件读，不写进配置、不进仓库，日志不打印密钥。
