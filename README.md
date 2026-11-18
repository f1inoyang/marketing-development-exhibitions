# Marketing Development · 全球市场拓展

按举办地和主题浏览全球展会，可切换中英文、打开官网或下载筛选后的清单。网站和两份 CSV 快照收录举办地当地当天仍在举行，以及在 2027 年 6 月 30 日或之前开幕的活动。跨月展会保留完整展期。

## 每日更新

GitHub Actions 每日北京时间 09:17 定时读取 `sources.json` 中启用的官网，也支持在 Actions 页面手动运行 **Refresh and publish exhibition calendar**。实际开始时间取决于 GitHub 排队情况。

采集器读取公开展会页、日历和可发现的主办方详情链接。发布程序只接受日期、城市、国家、主题及官方来源可识别的展会。它按名称、举办地和届次核对已有条目，保留已有收藏 ID 和双语资料。主办方明确公布的改期、取消或推迟会更新对应场次。网站访问失败时保留原有资料，在举办地的闭幕日全天结束后，条目从首页与清单移除。对于尚未识别时区的城市，保留至全球最后一个时区完成该日。

每次运行的审计包以 `exhibition-review-` 开头，可在 Actions 的运行记录下载，保存 30 天。其中 `collection-status.json` 记录各官网访问结果，`review-queue.json` 保存原始候选和证据，`publication-report.json` 逐条说明新增、更新、确认、待核或跳过的理由。目录中尚无日期、访问受限、只有宣传文案或身份不清晰的活动可能需要补充来源。覆盖会随主办方目录和解析规则扩大。

清洗通过后，工作流将首页、中英文 CSV 和可下载源码归档作为一次提交推送到 main，发起 Pages 发布，并逐一比较公开文件与本次生成的文件。仓库需开启 Actions 和 Pages，允许工作流写入 main 和 Pages。

## 本地运行

需要 Python 3.9 或更新的版本和系统时区数据库。使用仓库根目录执行以下命令。

```sh
python3 -m unittest discover -v
python3 collector.py --config sources.json --dry-run
python3 collector.py --config sources.json --timeout 8 --output-dir collection-run
python3 refresh.py --input collection-run --dry-run
python3 refresh.py --input collection-run
```

`--dry-run` 在内存中构建并校验，不写文件。已有数据可直接用 `python3 refresh.py --build-only` 重建。可用 `--as-of YYYY-MM-DD` 重放将各举办地固定在指定日以重放边界。

`exhibition-source.zip` 保存多文件页面、世界地图、正式资料及生成本次归档的脚本。在仓库根目录将归档内的 `exhibition-atlas/` 目录解压并改名为 `site/`，即可修改页面或补充 `site/data/verified-events.json` 的双语资料。构建会优先读取该目录，自动更新归档、单文件网站与双语 CSV。全新检出时，程序会直接读取已提交归档。根目录 `sources.json` 是正式官网配置。

© 2026 Marketing Team
