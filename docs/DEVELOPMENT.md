# 开发与调试

## 环境与入口

推荐 Windows + Python 3.13 + Tk，依赖版本见 `requirements-dev.txt`。从项目根目录执行 README 中的命令。界面入口为 `examples/control_gui.py`；厂家示例通常要求以 `examples` 为工作目录，不能假定所有脚本都具有 GUI 的路径处理逻辑。

`settings.py` 保存默认 CAN/编码器参数。GUI 的连接设置可用于选择当前设备。界面启动时保持未连接；不要为了验证界面而连接设备。Tk 缺失时使用包含 Tcl/Tk 的 Python 安装。

## 问题定位

| 现象 | 首先核对 |
| --- | --- |
| 无法连接 | 实际串口、端口占用、适配器、CAN 波特率、节点及接线 |
| 位置不一致 | 编码器型号/换算、当前轴、用户零点、反馈时间；停止运动后核查 |
| 目标预览改变但不运动 | 预览/拖动只修改目标，显式开始才发运动 |
| 长行程停滞/过冲 | 保存最小相关反馈片段和控制参数，参照 KNOWN_ISSUES；不要盲目重试 |
| 单元测试报 Tk 错误 | 是否有可用桌面、Tk 安装和足够窗口空间 |

## 代码分工

- `control_gui.py`：连接、动作队列、状态采集与停止流程。
- `common/utils.py`、`common/parameters.py`：CANopen 设备访问与参数。
- `position_panel.py`、`ui_theme.py`：控件与外观；必须保留真实状态含义。
- `stop_safety.py`、`travel_range.py`、`repeatability.py`：停止确认、行程和重复定位逻辑。
- `tools/verify_ui_layout.py`：暂停周期刷新后检查几何边界，并阻止 CAN 连接。
- `tools/verify_ui_responsiveness.py`：运行正常事件循环，检查日志压力下的软件响应。

## 打包一致性检查

使用与打包一致的 Python 版本和 `requirements-build.txt`：

```powershell
.\.xeryon-venv\Scripts\python.exe tools/verify_portable.py dist/XeryonControl.exe --source-dir examples --extra-module ui_theme --output portable-check.json
```

检查比较十个应用模块的编译代码，仅证明打包源码一致，不证明物理行为。二进制不放进 Git 历史；发布时放到 GitHub Release，诊断版须勾选 pre-release。

GitHub Actions 工作流见 `.github/workflows/offline-tests.yml`，仅 `contents: read`，不会访问实验设备。PR/主分支自动运行离线测试；实际 CI 结果以 Actions 页面为准。
