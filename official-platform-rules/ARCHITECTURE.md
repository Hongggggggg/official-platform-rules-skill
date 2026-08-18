# 动态平台规则知识库 V3 架构

## 目标

V3 在 V2 时点查询、证据链和人工复核上增加运行时平台档案、官方来源发现、覆盖审计和周期更新。SQLite 继续作为唯一事实库，每个平台/市场/主体组合保存在独立的 `data/profiles/<profile_id>/` 中。

## 数据流

```text
用户选择平台 + 已核验官方 HTTPS 入口
  -> profiles / discovery_runs / source_candidates
  -> robots.txt + sitemap + 官方目录 + 站内链接
  -> coverage_audits
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
- `profiles`：用户选择的平台、市场、主体和默认更新策略。
- `discovery_runs` / `source_candidates`：官方目录发现批次及已接纳、待复核和已拒绝链接。
- `coverage_audits`：主题覆盖、失败、待复核和新鲜度门槛报告。
- `update_schedules`：每日增量更新与每周重新发现状态。
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

- 档案默认记录每日 03:00（Asia/Shanghai）增量更新和每 7 天重新发现；用户确认后才创建外部调度。
- 普通查询仅定向刷新已命中且超过新鲜度阈值的来源。
- `--full` 明确要求全平台刷新，并忽略 ETag/Last-Modified 缓存验证器。
- 每个档案使用独立 `.sync.lock`，数据库、快照、锁和同步批次始终隔离。

## 迁移与恢复

`RuleDatabase.initialize()` 先执行幂等 V1 -> V2 证据链迁移，再执行 V3 动态档案迁移。新建知识库直接生成完整 V3 结构。

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
- `python scripts/verify_v2.py`（脚本名为兼容保留，当前验证 V3）
- SQLite `PRAGMA integrity_check`

验证报告必须记录 `schema_version`、`database_revision` 和 `last_sync_id`，
避免用旧报告描述已经变化的数据库。
