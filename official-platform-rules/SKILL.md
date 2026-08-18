---
name: official-platform-rules
description: 为任意电商平台动态搭建、更新和查询仅依据官方一手来源的本地规则知识库。首次询问某平台禁限售、入驻、上架、广告内容、履约、退款、费用、处罚、申诉、合同或政策变化时使用；也用于创建平台档案、发现官方知识中心、审计覆盖率和每日增量更新。不得用于选品、销量、竞品或第三方资讯结论。
---

# 动态官方平台规则知识库

不携带任何平台的成品规则库。先让用户选择平台与适用范围，再基于已核验官方入口动态建库。绝不把搜索摘要、第三方文章或模型记忆当作规则证据。

## 执行流程

1. 读取 [onboarding.md](references/onboarding.md)。先运行 `profiles`；没有档案时必须询问平台、市场、身份、注册地、履约与官方入口，不得直接回答规则。
2. 用户未提供官方入口时，使用网络搜索发现平台主站与帮助中心；核实平台归属后才将 HTTPS URL 传给 `onboard` 或 `profiles --add-official-url`。
3. 运行 `discover` 枚举 robots.txt、sitemap、目录和站内链接。同一已核验域名可自动接纳；新域名必须复核。
4. 运行 `build` 完成高风险优先的首次建库，再运行 `coverage`。状态为 `partial` 时明确展示缺口，不得声称“全面”或“最新”。
5. 回答前读取 [clarification.md](references/clarification.md) 和 [evidence-policy.md](references/evidence-policy.md)；使用 `query --profile`。历史事件必须传入 `--as-of`。
6. 按 [answer-format.md](references/answer-format.md) 输出结论、适用范围、官方 URL、生效日期、最后核验时间与知识库覆盖状态。
7. 更新、导入、复核或调度时读取 [operations.md](references/operations.md)。

## 命令

从本 Skill 目录运行：

```text
python scripts/cli.py profiles
python scripts/cli.py onboard --platform-name PLATFORM --market MARKET --official-url https://official.example/
python scripts/cli.py profiles --profile PROFILE_ID --add-official-url https://help.official.example/
python scripts/cli.py discover --profile PROFILE_ID
python scripts/cli.py build --profile PROFILE_ID
python scripts/cli.py coverage --profile PROFILE_ID
python scripts/cli.py update --profile PROFILE_ID
python scripts/cli.py update --all-due
python scripts/cli.py import-official --profile PROFILE_ID --source SOURCE_KEY --file imports/PROFILE_ID/export.html
python scripts/cli.py clarify --question "用户原问题"
python scripts/cli.py query --profile PROFILE_ID --question "已经澄清的问题"
python scripts/cli.py query --profile PROFILE_ID --question "历史事件问题" --as-of 2026-01-15
python scripts/cli.py digest --profile PROFILE_ID --since-days 1
python scripts/cli.py history --profile PROFILE_ID --rule-key RULE_KEY
python scripts/cli.py review --profile PROFILE_ID
python scripts/cli.py audit
```

若系统没有 `python` 命令，使用当前环境提供的 Python 3.11+ 可执行文件。所有命令均输出 UTF-8 JSON，便于可靠读取。

## 强制约束

- 普通问答一次只查询一个平台档案数据库。跨平台比较必须分别查询，再并列呈现。
- 所有平台档案的来源清单、数据库、快照、锁、复核队列和更新记录必须隔离。
- 证据不足时明确回答“官方资料暂未确认”，不得用第三方内容补全。
- 规则超过其新鲜度阈值时先定向同步；同步失败时展示最后核验时间，不得声称“最新”。
- 默认每日 03:00（Asia/Shanghai）增量更新，每 7 天重新发现官方目录。只有用户确认后才创建外部调度；否则每次使用时补做逾期更新。
- 涉及历史事件时必须传入 `--as-of YYYY-MM-DD`，不得用当前规则替代事件发生时规则。
- 用户纠正前提后废弃旧检索结果，从澄清步骤重新开始。
- 不保存 Cookie、Token、密码或浏览器凭证。登录后官方内容只允许通过用户授权的可见页面或正式导出导入。
- 不调用本项目其他 Skill、数据库、缓存或配置。


