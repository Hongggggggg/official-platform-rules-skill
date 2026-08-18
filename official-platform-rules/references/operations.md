# 运行与更新

## 初始化与首次建库

从 `official-platform-rules/` 目录运行：

```text
python scripts/cli.py profiles
python scripts/cli.py onboard --platform-name PLATFORM --market MARKET --official-url OFFICIAL_HTTPS_URL
python scripts/cli.py discover --profile PROFILE_ID
python scripts/cli.py build --profile PROFILE_ID
python scripts/cli.py coverage --profile PROFILE_ID
python scripts/cli.py audit
```

每个平台、市场和主体组合分别写入 `data/profiles/<profile_id>/`。任一档案失败不影响其他档案。

## 周期更新

档案默认记录 `Asia/Shanghai 03:00` 每日更新计划。用户确认后，可在当前环境创建调度任务执行：

```text
python scripts/cli.py update --all-due
```

未创建外部调度时，`query` 会在使用时补做逾期更新：

- 回答时：`query` 检查档案更新计划和过期命中来源。
- 每 7 天自动重新枚举 robots.txt、sitemap 和官方目录。
- 明确要求重新发现时：执行 `update --profile PROFILE_ID --rediscover`。
- `--no-refresh` 仅用于离线检查和固定数据库修订号上的验证。
- 各档案必须分别更新，`--all-due` 也只是依次执行隔离写任务。

`sync` 全部成功返回 0；任一来源失败返回 3。每个平台使用独立写锁，
同步记录必须保存 `schema_version`、来源集合和数据库修订号。

## 动态页面与正式导出

正文少于质量门槛的登录页、验证码页或 JavaScript 空壳会被拒绝，不会成为 `current` 规则。

官方页面若只返回动态空壳：

1. 在官方页面中取得用户可见的完整 HTML 或纯文本正式导出。
2. 将文件放入本 Skill 的 `imports/<profile_id>/`，不得包含 Cookie、Token 或密码。
3. 使用预配置的官方来源键导入：

```text
python scripts/cli.py import-official --profile PROFILE_ID --source SOURCE_KEY --file imports/PROFILE_ID/export.html
```

导入文件必须位于本 Skill 内；URL 与适用范围仍由该平台配置决定，不能借此导入第三方材料。

## 复核

```text
python scripts/cli.py review --profile PROFILE_ID
python scripts/cli.py history --profile PROFILE_ID --rule-key RULE_KEY
```

以下情况不得自动选边：无日期变化、低优先级来源与高优先级来源冲突、章节突然消失、未来生效范围不清。
