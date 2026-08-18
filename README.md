# Official Platform Rules Skill

一个可安装的 Codex Skill。首次使用先选择任意电商平台，再动态发现官方来源、建立本地知识库并持续更新。安装包不附带任何平台的成品规则数据。

## 能力边界

- 为任意平台和市场创建隔离档案，发现官方 sitemap、目录和站内规则页。
- 管理官方来源、证据快照、适用市场与主体、生效时间和最后核验时间。
- 支持当前规则查询、历史时点查询、规则冲突、过期风险、人工复核和可追溯引用。
- 不用于选品、销量、竞品、第三方资讯或法律意见。

## 仓库结构

```text
official-platform-rules/
├── SKILL.md              # Skill 入口与强制工作流
├── agents/openai.yaml   # Codex 界面元数据
├── config/               # 平台、来源与检索配置
├── references/           # 澄清、证据、回答与运维规范
├── scripts/              # 采集、SQLite 建库、检索和审计脚本
├── tests/                # 标准库 unittest 测试
└── validation/           # 小型可版本化查询用例
```

运行时生成的 SQLite 数据库、HTML 快照、导入文件、复核数据、报告和生成型验证语料不纳入 Git。

## 安装

需要 Python 3.11+ 和支持 FTS5 的 SQLite。Skill 本身仅使用 Python 标准库。

```powershell
git clone git@github.com:Hongggggggg/official-platform-rules-skill.git
Copy-Item -Recurse -Force `
  .\official-platform-rules-skill\official-platform-rules `
  "$env:USERPROFILE\.codex\skills\official-platform-rules"
```

在 Codex 中使用：

```text
使用 $official-platform-rules 为我选择的电商平台搭建官方规则知识库。
```

## 快速验证

```powershell
Set-Location .\official-platform-rules
python -m unittest discover -s tests -v
python scripts/cli.py audit
python scripts/cli.py profiles
python scripts/cli.py onboard --platform-name PLATFORM --market MARKET --official-url OFFICIAL_HTTPS_URL
python scripts/cli.py discover --profile PROFILE_ID
python scripts/cli.py build --profile PROFILE_ID
python scripts/cli.py coverage --profile PROFILE_ID
```

同步官方规则需要网络访问；无需网络的初始化、查询与审计可直接在本地运行。详细数据库、迁移和恢复设计见 [ARCHITECTURE.md](official-platform-rules/ARCHITECTURE.md)。

## 证据与安全

- 仅允许官方一手来源；搜索摘要和第三方页面不能成为规则证据。
- 不得提交 Cookie、Token、密码、受限页面或浏览器凭证。
- 证据不足、规则冲突或页面过期时，必须明确披露不确定性。
- 历史事件必须使用 `--as-of YYYY-MM-DD`，不得用当前规则替代历史规则。
