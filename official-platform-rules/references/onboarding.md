# 平台档案与首次建库

## 首次交互

1. 先运行 `python scripts/cli.py profiles`。
2. 没有档案时，每轮询问 1～3 项，直到确认平台名称、国家/站点、身份、注册地和履约方式。
3. 询问用户是否已知官网、Seller Center、帮助中心或政策入口。
4. 用户未提供时，搜索平台官网及其官方帮助中心。搜索只负责发现 URL，不采纳搜索摘要。
5. 核验平台主站链接、品牌和法律主体一致性后，才把 URL 标记为已核验官方入口。

## 档案命令

```text
python scripts/cli.py onboard --platform-name "Amazon" --market "US" --seller-origin "CN" --actor-type seller --seller-type cross-border --fulfillment FBA --official-url https://sellercentral.amazon.com/help/
python scripts/cli.py profiles --profile PROFILE_ID --add-official-url https://www.amazon.com/gp/help/
python scripts/cli.py profiles --activate PROFILE_ID
python scripts/cli.py profiles --archive PROFILE_ID
```

`--official-url` 的语义是“已由执行者核验官方归属”，不得直接传入用户未核验的第三方 URL。

## 生命周期

- `needs_official_sources`：尚无已核验官方入口。
- `ready_for_discovery`：可执行目录发现。
- `building`：首次同步进行中。
- `partial`：可查询已核验内容，但仍有覆盖、失败、待复核或新鲜度缺口。
- `complete`：已通过当前覆盖与新鲜度门槛。
- `archived`：可恢复归档，不参与自动更新。
