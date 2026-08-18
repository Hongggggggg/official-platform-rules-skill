---
name: official-platform-rules
description: 仅依据官方来源查询、核验并持续跟踪 TikTok Shop 与 Ozon 平台规则。用户询问禁限售、商品与内容要求、上架运营、广告、达人、履约、订单、退款退货、费用、处罚、申诉、合同或官方政策变化时使用；问题范围不明确时必须多轮澄清。不得用于市场销量、选品、竞品或第三方资讯分析。
---

# 官方平台规则知识库

仅使用已验证的官方来源回答 TikTok Shop 与 Ozon 规则问题。绝不把搜索摘要、第三方文章或模型记忆当作规则证据。

## 执行流程

1. 读取 [clarification.md](references/clarification.md)，判断平台、市场、身份、履约方式、类目、问题类型和事件日期是否足以唯一确定适用规则。
2. 信息不足时每轮只问 1～3 个最关键问题。继续确认，直到问题清楚；不得猜测缺失前提。
3. 确认 TikTok Shop 后读取 [tiktok-scope.md](references/tiktok-scope.md)；确认 Ozon 后读取 [ozon-scope.md](references/ozon-scope.md)。不得同时读取另一平台资料，除非用户明确要求跨平台比较。
4. 读取 [evidence-policy.md](references/evidence-policy.md)，使用 `scripts/cli.py` 查询当前有效规则。需要更新时先运行目标平台的 `sync`。
5. 读取 [answer-format.md](references/answer-format.md)，按固定结构回答并附官方 URL、适用范围、发布日期或生效日期、最后核验时间。
6. 执行同步、正式导出导入、复核或调度时读取 [operations.md](references/operations.md)。

## 命令

从本 Skill 目录运行：

```text
python scripts/cli.py init --platform tiktok
python scripts/cli.py init --platform ozon
python scripts/cli.py sync --platform tiktok
python scripts/cli.py sync --platform ozon
python scripts/cli.py import-official --platform ozon --source SOURCE_KEY --file imports/ozon/export.html
python scripts/cli.py clarify --question "用户原问题"
python scripts/cli.py query --platform tiktok --question "已经澄清的问题"
python scripts/cli.py query --platform ozon --question "已经澄清的问题"
python scripts/cli.py query --platform tiktok --question "历史事件问题" --as-of 2026-01-15
python scripts/cli.py digest --platform tiktok --since-days 1
python scripts/cli.py digest --platform ozon --since-days 1
python scripts/cli.py history --platform tiktok --rule-key RULE_KEY
python scripts/cli.py status --platform ozon
python scripts/cli.py review --platform ozon
python scripts/cli.py review-decide --platform ozon --rule-version-id 123 --decision approve --reason "官方日期与范围已人工确认" --reviewer "name"
python scripts/cli.py audit
```

若系统没有 `python` 命令，使用当前环境提供的 Python 3.11+ 可执行文件。所有命令均输出 UTF-8 JSON，便于可靠读取。

## 强制约束

- 普通问答一次只查询一个平台数据库。跨平台比较必须分别查询，再并列呈现。
- TikTok 与 Ozon 的配置、采集器、数据库、快照、复核队列和变更记录互相独立。
- 证据不足时明确回答“官方资料暂未确认”，不得用第三方内容补全。
- 规则超过其新鲜度阈值时先定向同步；同步失败时展示最后核验时间，不得声称“最新”。
- 不创建定时任务；没有用户问题或明确同步请求时保持不动。
- 涉及历史事件时必须传入 `--as-of YYYY-MM-DD`，不得用当前规则替代事件发生时规则。
- 用户纠正前提后废弃旧检索结果，从澄清步骤重新开始。
- 不保存 Cookie、Token、密码或浏览器凭证。登录后官方内容只允许通过用户授权的可见页面或正式导出导入。
- 不调用本项目其他 Skill、数据库、缓存或配置。


