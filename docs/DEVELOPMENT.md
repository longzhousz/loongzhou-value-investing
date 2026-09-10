# 架构与开发验证

本项目从仓库根目录运行，入口 `scripts/value.ps1` 设置 `PYTHONPATH=src`，不需要安装其他桌面程序。Python 3.12和固定依赖见 `requirements-value-lock.txt`；允许升级范围见 `requirements-value.txt`。

## 模块职责

`domain.py` 只依赖标准库，负责财务安全、六项目标、历史窗口、同行及排序。`providers.py` 适配BaoStock、AKShare和东方财富，`worker.py` 将真实请求隔离在可终止进程。`pipeline.py` 组织采集、缓存和筛选，`storage.py` 保存SQLite快照及原始材料。`reports.py` 输出Excel、HTML和JSON，`research.py` 调用Codex并核验证据，`evidence_review.py` 追加人工复核；`cli.py` 管理命令、文件锁和补跑。

正式快照保存规则内容与哈希、观测和结果，禁止更新删除。缓存、AI队列及重试状态可更新；研究版本只追加。不要用数据库迁移覆盖历史判断。

## 本地验证

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_value.ps1
.\.venv-value\Scripts\python.exe -m pytest
.\scripts\value.ps1 --data-dir exports\value-demo demo
```

测试使用合成数据、临时目录和替身，不要求网络、交易账号、Codex登录或订阅额度。CI执行Windows Python 3.12离线用例，依赖安装需要网络。CI不运行真实扫描、不注册Windows任务、不调用AI。

真实数据接口单独验证，以下命令会联网：

```powershell
.\.venv-value\Scripts\python.exe scripts\value_data_probe.py baostock
.\.venv-value\Scripts\python.exe scripts\value_data_probe.py akshare
.\scripts\value.ps1 scan --no-ai
.\.venv-value\Scripts\python.exe scripts\check_value_snapshot.py --snapshot <真实快照ID>
```

独立复算读取本地真实快照，不联网；首次克隆没有这些快照，不能直接复现历史验证文件。真实取数结果受交易日、接口变化和免费数据覆盖影响，验证记录明确区分计算一致性和原始数据可靠性。

修改阈值、口径或资格规则时更新 `configs/value_rules.json` 的版本和使用说明，并验证临界值、缺失值、非正PE、3/5年完整性和同档排序。不得用测试预期放宽正式数据要求。提交前运行 `git diff --check`，确认未暂存运行产物或凭据。

## 安装迁移

先停止旧定时任务并等待运行结束，备份整个数据目录，再安装新目录并复制备份。人工运行 `status` 和离线复算确认数据可读后，重新注册同名Windows任务。仓库更新不自动迁移数据或改写任务路径。
