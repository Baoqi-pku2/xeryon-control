# 开发与调试

## 环境与入口

项目使用 uv 管理 项目虚拟环境、PySide6 和依赖，解释器版本见 `.python-version`，运行依赖和打包依赖组见 `pyproject.toml`，实际解析版本见 `uv.lock`。从根目录运行 `uv sync --locked` 准备环境。修改依赖后运行 `uv lock`，将配置和锁文件一起提交。界面入口为 `src/xeryon_control/ui/main_window.py`。厂家示例通常要求以 `examples` 为工作目录，可在该目录用 `..\.venv\Scripts\python.exe` 执行。

`src/xeryon_control/config.py` 保存默认 CAN/编码器参数。GUI 的连接设置可用于选择当前设备。界面启动时保持未连接；不要为了验证界面而连接设备。Qt Widgets 及其运行库由项目的 uv 锁文件管理。

已有匹配的基础 Python 时，`uv sync --locked --no-python-downloads --no-cache --link-mode copy` 可将依赖复制到项目的 `.venv`，避免写入共享缓存。打包时加上 `--group build`。

## 问题定位

| 现象 | 首先核对 |
| --- | --- |
| 无法连接 | 实际串口、端口占用、适配器、CAN 波特率、节点及接线 |
| 位置不一致 | 编码器型号/换算、当前轴、用户零点、反馈时间；停止运动后核查 |
| 目标预览改变但不运动 | 预览/拖动只修改目标，显式开始才发运动 |
| 长行程停滞/过冲 | 保存最小相关反馈片段和控制参数，参照[历史已知问题](history/KNOWN_ISSUES.md)；不要盲目重试 |
| Qt 平台插件加载失败 | 核对项目环境和 Qt 平台插件，离线测试可指定 QT_QPA_PLATFORM=offscreen |

## 代码分工

应用包位于 `src/xeryon_control/`，通过 uv 安装。命令行入口为 `python -m xeryon_control`。

| 包内路径 | 职责 |
| --- | --- |
| `domain/` | 坐标换算、各轴零点、设备标称尺寸和分段规划，可直接脱离 GUI 和设备测试 |
| `application/session.py`、`position.py` | 界面独立的设备会话、坐标输入和位置反馈显示状态 |
| `application/commands.py` | 普通命令准入、后台执行、取消代次，以及带取消检查的设备写入 |
| `application/feedback.py` | 轮询节流、SDO/PDO 反馈、目标进度、到位与异常复核 |
| `application/linked_motion.py` | 双轴零点、方向与限位换算、同步推进及两轴偏差监测 |
| `application/stop.py` | 独立 STOP 线程、逐轴失能、停稳确认与超时状态 |
| `application/calibration.py`、`motion.py`、`repeatability.py` | 端点采样、起点变化确认数据、重复定位流程 |
| `hardware/` | CANopen 配置与读写、驱动状态判断、设定点握手及停稳确认 |
| `ui/` | Qt Widgets 主控制台、高级设置、弹窗与 QTimer 事件桥接。主窗口持有独立的 ControllerSession |
| `reporting/` | 统一日志后台、文件轮转、异常记录和运动轨迹 JSONL 格式 |
| `resources/eds/` | 随包发布的 EDS。厂家[对象字典说明](reference/xeryon_xla_5_eds_docu.txt)位于参考资料目录 |

界面布局脚本在 Windows 桌面使用默认 Qt 平台，核对实际窗口宽度、水平滚动和快捷按钮可达性。单元测试可使用离屏平台。

离线测试在 `tests/`。从根目录运行 `uv run --locked python -m unittest discover -s tests`。厂家示例和实机通信脚本保留在 `examples/`。

### 验证等待设备回复时的 STOP

运行 `uv run --locked python -m unittest -v tests.test_stop_interrupt`。测试让普通命令持有会话 I/O 锁并等待模拟编码器的 SDO 回复。STOP 先取消旧命令，使用独立线程向两个轴发送失能帧，再取得 I/O 锁，逐轴核验状态字和位置稳定性。

测试分别释放和持续阻塞回复。前者要求两轴停稳后才报告成功；后者要求锁等待超时后保留“停止未确认”并阻止再次启动。两种情况下，旧命令都不能在迟到的回复之后写入目标或重新使能。测试使用模拟传输，实机停止延迟需现场测量。

## 统一日志

`reporting/logging_service.py` 提供进程内单例。正式入口在导入主窗口前调用 `start_logging()`，退出时调用 `shutdown_logging()`；异常退出还由 `atexit` 尝试收尾。正常日志使用 `logging.getLogger(__name__)`，不要在模块导入时调用 `basicConfig()` 或自行安装文件 Handler。

界面 `_log()` 和后台 `_post("log", ...)` 在产生消息时接入统一日志，UI 消息队列的显示操作不重复写日志。界面继续展示简短操作摘要；底层详细记录和异常堆栈在文件中。记录包含毫秒时间、级别、线程及模块，命令、STOP、反馈和界面动作异常保留 traceback。`sys.excepthook`、`threading.excepthook` 捕获未处理的 Python 异常；不保证捕获原生崩溃、系统强杀或断电。

运行日志单文件上限 5 MiB，保留 3 个备份；每次启动使用独立目录，避免多个进程争用轮转文件。启动时尝试保留最近 20 个会话的运行日志，只清理日志目录中的 `runtime.log*`，占用文件跳过。运动轨迹位于本次会话目录的 `motion/motion_*.jsonl`，不参与自动清理，因此含轨迹的旧会话目录会保留。测试报告仍在 `results` 中且不自动清理，旧版轨迹保留原位置。

写入线程使用 8192 条上限的队列，生产者 `put_nowait`，队列满则丢弃新记录、统计数量并提示记录不完整，不等待磁盘。每批最多 256 条、聚合窗口 200 ms，运动轨迹按文件成批追加；JSONL 保留原有字段并增加 `session_id`。设备线程只生成记录和入队，不开关轨迹文件。运行日志仍逐条轮转写入，但全部发生在日志线程。

写盘失败通过独立内存通知交给界面，防止错误日志形成递归。失败批次不重试，后续批次会继续尝试；队列排空只代表完成写入尝试，不保证失败记录保存。退出最多等待 2 秒，超时不阻塞退出。未执行 `fsync`，不承诺断电不丢数据。磁盘阻塞时优先保证控制线程与 STOP 可运行。

`flush()` 仅供离线测试或导出后的核验，不得在设备等待、STOP 或反馈路径调用。零点恢复的 QSettings 事件保存及测试报告保留各自数据格式；此模块不增加 CAN 读取，也不改变已有每约 2 秒的诊断采样。

验证：`uv run --locked python -m unittest -v tests.test_logging_service tests.test_feedback tests.test_stop_interrupt`，覆盖统一文件、堆栈、轮转、保留策略、写盘失败、轨迹顺序和退出排空；磁盘写入阻塞并填满日志队列时，模拟两个轴等待 SDO 的 STOP 仍需先发关闭帧，迟到回复不得重启运动。

## 打包一致性检查

使用相同的锁文件和 `build` 依赖组检查[打包命令](../README.md#windows-打包)的输出：

```powershell
uv run --locked --group build python tools/verify_portable.py release/portable/XeryonControl.exe --source-dir src --output portable-check.json
```

检查逐个比较当前应用包内的模块的编译代码，仅证明打包源码一致，不证明物理行为。二进制不放进 Git 历史；发布时放到 GitHub Release，诊断版须勾选 pre-release。

GitHub Actions 工作流见 `.github/workflows/offline-tests.yml`，仅 `contents: read`，不会访问实验设备。PR/主分支自动运行离线测试；实际 CI 结果以 Actions 页面为准。
