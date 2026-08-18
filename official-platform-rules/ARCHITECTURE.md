# 平台规则知识库 V2 架构

## 目标

V2 保持本地、低依赖和官方证据优先，同时补齐时点查询、结构化适用范围、
证据链、抓取审计、人工复核和可扩展全文检索。SQLite 继续作为唯一事实库，
原始官方页面继续保存在各平台独立的 `data/<platform>/snapshots/` 中。

## 数据流

```text
官方 HTTPS 来源
  -> fetch_runs（请求、缓存验证器、结果或错误）
  -> snapshots（原始快照及原始内容哈希）
  -> extracted_sections（解析器版本、章节和章节哈希）
  -> rule_versions（规则语义版本）
  -> effective_intervals（有效期与观测时间）
  -> evidence_links（规则版本到快照章节）
  -> rules_fts_v2（BM25候选召回及中文二元索引）
  -> 结构化适用范围 + 时点过滤 + 业务词典加权
```

## 核心表

- `sources`：配置中的官方来源、风险、缓存验证器和最后抓取状态。
- `fetch_runs`：每个来源的每次网络访问，关联平台同步批次。
- `snapshots`：页面级证据，记录正文哈希、原始哈希、文件路径和解析器版本。
- `extracted_sections`：从快照提取出的可引用章节。
- `rule_versions`：版本化规则正文及兼容字段。
- `applicability_scopes`：市场、主体来源、角色、店铺类型、履约、类目、项目和订单状态。
- `effective_intervals`：`valid_from`、`valid_to`、`observed_at`、`retired_at`。
- `evidence_links`：规则版本、快照和章节之间的可追溯关系。
- `review_decisions`：人工批准、拒绝、撤回或保留当前版本的审计记录。
- `sync_runs`：同步批次、来源集合、schema版本和数据库修订号。
- `rules_fts_v2`：包含历史版本的全文索引；生命周期和时点过滤在召回后执行。

## 检索

检索顺序为：

1. 校验平台边界和 `as_of_date`。
2. 使用 FTS5/BM25 及中文字符二元词生成候选。
3. 使用 `config/query-concepts.json` 的业务词典提高官方来源和章节权重。
4. 按结构化适用范围过滤。
5. 按 `valid_from <= as_of_date < valid_to` 选择当时有效版本。
6. 返回官方 URL、快照、章节哈希、解析器版本和核验时间。

BM25 和业务词典只负责发现证据，不生成规则事实。没有匹配到官方规则正文时必须拒答。

## 更新与并发

- 不创建定时任务。只有用户提出规则问题或明确要求同步时才更新。
- 普通查询仅定向刷新已命中且超过新鲜度阈值的来源。
- `--full` 明确要求全平台刷新，并忽略 ETag/Last-Modified 缓存验证器。
- 每个平台使用独立 `.sync.lock`，防止多个写任务同时修改同一 SQLite 数据库。
- TikTok 与 Ozon 的数据库、快照、锁和同步批次始终隔离。

## 迁移与恢复

`RuleDatabase.initialize()` 自动执行幂等 V1 -> V2 迁移：

1. 保留旧表和旧字段。
2. 新建 V2 表及列。
3. 从旧来源回填结构化适用范围。
4. 从历史版本回填有效期。
5. 从已有快照回填章节和证据链接。
6. 重建 `rules_fts_v2`。
7. 写入 `migration_history(version=2)`。

迁移前备份位于：

```text
data/backups/pre-v2-2026-07-26/
```

恢复时应先停止所有同步进程，然后以备份数据库替换对应平台数据库。原始快照未被迁移删除。

## 验证门槛

- `python -m unittest discover -s tests -v`
- `python scripts/validate_queries.py`
- `python scripts/validate_question_corpus.py`
- `python scripts/cli.py audit`
- `python scripts/verify_v2.py`
- SQLite `PRAGMA integrity_check`

验证报告必须记录 `schema_version`、`database_revision` 和 `last_sync_id`，
避免用旧报告描述已经变化的数据库。
