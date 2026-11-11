# 开发与调试

## 环境与入口

项目使用 uv 管理 Python 3.13 和依赖，解释器版本见 `.python-version`，运行依赖和打包依赖组见 `pyproject.toml`，实际解析版本见 `uv.lock`。从根目录运行 `.\project_uv.cmd sync --locked` 准备环境。修改依赖后运行 `.\project_uv.cmd lock`，将配置和锁文件一起提交。界面入口为 `src/xeryon_control/ui/main_window.py`。厂家示例通常要求以 `examples` 为工作目录，可在该目录用 `..\.venv\Scripts\python.exe` 执行。

`src/xeryon_control/config.py` 保存默认 CAN/编码器参数。GUI 的连接设置可用于选择当前设备。界面启动时保持未连接；不要为了验证界面而连接设备。Tk 缺失时使用包含 Tcl/Tk 的 Python 安装。

## 问题定位

| 现象 | 首先核对 |
| --- | --- |
| 无法连接 | 实际串口、端口占用、适配器、CAN 波特率、节点及接线 |
| 位置不一致 | 编码器型号/换算、当前轴、用户零点、反馈时间；停止运动后核查 |
| 目标预览改变但不运动 | 预览/拖动只修改目标，显式开始才发运动 |
| 长行程停滞/过冲 | 保存最小相关反馈片段和控制参数，参照 KNOWN_ISSUES；不要盲目重试 |
| 单元测试报 Tk 错误 | 是否有可用桌面、Tk 安装和足够窗口空间 |

## 代码分工

应用包位于 `src/xeryon_control/`，通过 uv 安装。命令行入口为 `python -m xeryon_control`。

| 包内路径 | 职责 |
| --- | --- |
| `domain/` | 坐标换算、各轴零点、机械行程和分段规划，可直接脱离 Tk 和设备测试 |
| `application/` | 端点采样流程、起点变化确认数据、重复定位流程 |
| `hardware/` | CANopen 配置与读写、驱动状态判断、设定点握手及停稳确认 |
| `ui/` | Tk 控件、弹窗和主窗口；主窗口仍协调后台命令、反馈和 STOP |
| `reporting/` | 结果目录与运动轨迹 JSONL 格式 |
| `resources/eds/` | 随包发布的 EDS 及对象字典说明 |

离线测试在 `tests/`。从根目录运行 `.\project_uv.cmd run --locked python -m unittest discover -s tests`。厂家示例和实机通信脚本保留在 `examples/`。

## 打包一致性检查

使用相同的锁文件和 `build` 依赖组检查打包脚本的输出：

```powershell
.\project_uv.cmd run --locked --group build python tools/verify_portable.py release/portable/XeryonControl.exe --source-dir src --output portable-check.json
```

检查逐个比较当前应用包内的模块的编译代码，仅证明打包源码一致，不证明物理行为。二进制不放进 Git 历史；发布时放到 GitHub Release，诊断版须勾选 pre-release。

GitHub Actions 工作流见 `.github/workflows/offline-tests.yml`，仅 `contents: read`，不会访问实验设备。PR/主分支自动运行离线测试；实际 CI 结果以 Actions 页面为准。
