# Marketing Development · 全球市场拓展

全球展会双语导航网站，收录 86 场从 2026 年 11 月 5 日之后至 2027 年 6 月 30 日开幕的已核验活动。以举办地分区，覆盖金融科技、银行、电信、数字货币、包装及相关身份安全主题。

`index.html` 是自包含的线上页面；打开后可按地区、主题和月份筛选，查看官网和核验说明，并下载当前筛选的 CSV 或 JSON。中英文清单快照为 `events-zh.csv` 和 `events-en.csv`。记录随时间自动隐藏已经开始的活动；快照不会自动变化。

`exhibition-source.zip` 包含完整多文件源码、数据、开发与构建脚本。展会资料仅覆盖截至 2026 年 11 月 5 日找到并核验的公开来源，不代表全球目录已穷尽，主办方可能变更日期和安排。

## 官网候选监测

`collector.py` 按 `sources.json` 中已配置的官方网站读取公开的结构化 Event 数据。工作流每天约北京时间 09:17 运行，候选和状态作为工作流 artifact 保存 30 天，需人工核验后才更新网站；它不是全网搜索，也不会自动改写已发布展会或确认参会资格。

本地校验：`python3 -m unittest test_collector`；`python3 collector.py --config sources.json --dry-run`。核验后更新网站请编辑源码包的正式 JSON、重新构建，再更新仓库中的 `index.html` 及清单快照。

© 2026 Marketing Team
