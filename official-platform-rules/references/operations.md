# 运行与更新

## 初始化

从 `official-platform-rules/` 目录运行：

```text
python scripts/cli.py init --platform tiktok
python scripts/cli.py init --platform ozon
python scripts/cli.py audit
```

TikTok 与 Ozon 分别写入 `data/tiktok/` 和 `data/ozon/`。任一平台失败不影响另一平台。

## 按需更新

本项目不创建定时任务。没有用户问题或明确同步请求时保持不动：

- 回答时：`query` 识别过期命中来源并定向刷新。
- 明确要求全面更新某平台时：执行该平台 `sync --full`。
- `--no-refresh` 仅用于离线检查和固定数据库修订号上的验证。
- TikTok 与 Ozon 必须分别更新，不建立跨平台联合写任务。

`sync` 全部成功返回 0；任一来源失败返回 3。每个平台使用独立写锁，
同步记录必须保存 `schema_version`、来源集合和数据库修订号。

## 动态页面与正式导出

正文少于质量门槛的登录页、验证码页或 JavaScript 空壳会被拒绝，不会成为 `current` 规则。

Ozon 页面若只返回动态空壳：

1. 在官方页面中取得用户可见的完整 HTML 或纯文本正式导出。
2. 将文件放入本 Skill 的 `imports/ozon/`，不得包含 Cookie、Token 或密码。
3. 使用预配置的官方来源键导入：

```text
python scripts/cli.py import-official --platform ozon --source partner-delivery --file imports/ozon/partner-delivery.html
```

导入文件必须位于本 Skill 内；URL 与适用范围仍由该平台配置决定，不能借此导入第三方材料。

## 复核

```text
python scripts/cli.py review --platform tiktok
python scripts/cli.py review --platform ozon
python scripts/cli.py history --platform tiktok --rule-key RULE_KEY
```

以下情况不得自动选边：无日期变化、低优先级来源与高优先级来源冲突、章节突然消失、未来生效范围不清。
