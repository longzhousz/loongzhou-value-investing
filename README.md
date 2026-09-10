# 龙舟价值选股

在 Windows 笔记本本地运行的上交所 A 股价值研究筛选程序。使用免费数据、Python 和 SQLite，输出 Excel 候选表及离线 HTML 报告；不预测短期涨跌、不自动交易。

遵循“规则决定资格，证据决定把握，标签解释原因，排序决定先研究谁”。金融、强周期和缺少专门规则的行业标为暂未覆盖。候选最多20家，不足5家如实输出。

## 快速开始

安装 Python 3.12（Windows Python Launcher `py`）或 uv，下载或克隆本仓库，在仓库根目录打开 PowerShell：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_value.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\value.ps1 doctor
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\value.ps1 --data-dir exports\value-demo demo
```

`demo` 使用合成数据，完全离线。真实扫描运行 `scripts\value.ps1 scan --no-ai`；首次可能耗时数小时。报告默认写入 `exports/value-investing/reports`，数据库、原始数据和证据均在本地保存。

需要自动研究时，先安装官方 Codex CLI，以自己的 ChatGPT 账号登录，再运行 `scripts\value.ps1 doctor --probe-ai` 验证。`scan` 默认为候选排队研究；额度不足或失败时仍输出基础报告，不切换额外付费 API。

## 当前规则

六项等权软目标：`0 < PE(TTM) <= 20`、总市值大于100亿元、毛利率大于25%、净利率大于10%、股息率大于3%、最近完整年度ROE大于10%。不要求全部达标，达标越多排序越靠前；未知不计达标。毛利率、净利率和ROE使用最近完整年度，股息率使用近12个月已实施税前现金分红。

正式资格仍要求基本财务安全、至少完整有效3年数据，以及同一3年或5年窗口内价格与PE/PB历史低位，同时相对质量可比同行不贵。AI意见和事件标签不能豁免资格，也不自动加分。全部阈值与口径见[使用及规则说明](VALUE_INVESTING.md)和[规则配置](configs/value_rules.json)。

## 文档

- [安装、运行、定时、指标口径与故障排查](VALUE_INVESTING.md)
- [架构与开发验证](docs/DEVELOPMENT.md)
- [真实数据验证记录及已知限制](docs/VALUE_VALIDATION.md)
- [版本变更](CHANGELOG.md)
- [数据与凭据管理](SECURITY.md)

Windows 定时配置见使用说明，默认北京时间周三、周六20:00，登录后补跑最近遗漏时段。注册任务会更新同名 `Loongzhou-ValueInvesting` 任务；一台电脑保留一个正式运行目录，迁移前备份数据。

## 验证与边界

2026-09-10开发机记录：69项离线测试通过；真实扫描取得2317家上海股票名录，322家公司取得行情与财务，形成7家候选。覆盖数不代表交易所官方完整名录，历史结果不代表当前候选。独立复算326个有效窗口和7家候选同行通过，详见验证记录。运行数据和公告PDF不随源码分发，新安装须自行取数。

这是一项研究辅助工具。首版没有完整历史回测；免费数据可能缺失或口径变化；审计、诉讼、关联交易等未核验事项保留未知，不能声称已排除所有价值陷阱。项目未授予开源许可证；公开可见也不等于授权任意再分发。第三方依赖和数据遵循各自条款。
