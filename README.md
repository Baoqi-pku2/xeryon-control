# Xeryon Control · 电极推进器控制台

基于 Xeryon CANopen 示例扩展的 Python / Tkinter 桌面控制软件，用于 XLA 推进器控制、状态观察和问题复现。

**已发布版本：20261106.38，诊断版，待实机验收。** 已知长行程停滞、过冲、启停位置变化仍待解决。离线测试通过不代表这些实机问题已经修复；详见 [已知问题](docs/KNOWN_ISSUES.md)。

## 当前源码的零点与分段进退

驱动失能时，把推杆放在实物收回端点，松手并待其静止，再点「收回端点置零」。软件短暂使能当前轴，保持 Halt 且不触发新运动目标，读取编码器后失能。使能期间和失能停稳后的读数一致（允许 1 计数误差）才记录用户零点。过程中可按 STOP 取消。单轴绝对目标和分段坐标以此处为 0 µm。

置零后再次手调推杆，部分固件可能直到下次使能才报告新位置。控制台「开始运动」检测到起点变化超过约 20 µm 时，会先停止并核验失能。弹窗列出新起点、原目标、带方向的剩余位移和速度。点「继续」会再次读取位置、校验目标与限位，并按新的实际起点运动；点「取消」会同时取消排队的快捷步骤。

切换轴会恢复该轴本次连接的标定，双轴联动使用已确认的联动零点并逐轴检查机械行程。断开连接会结束本次标定，控制器重启或重新找参考点后须重新核对机械零点。通信异常会中止流程；恢复反馈后点击「复核零点」，程序核对各节点身份、故障前位置和失能后的稳定位置。复核通过后由操作者重新发起运动。

在「高级设置 → 高级控制」填写工作起点、快慢切换点和推进终点。默认分别为 50、10050、13000 µm。坐标顺序决定推进方向。页面显示从当前位置推进或退出的下一段。STOP 后可从已确认的位置继续，例如在 11000 µm 点击分段推进只执行到 13000 µm 的慢速段。退出时先慢速越过切换点，确认所有参与轴已离开切换窗口后才加速。流程范围外会拦截，已在对应终点窗口内无需运动。

这些变更尚待实机验收。下方截图和下载对应已发布版。

## 最新版界面

![Xeryon Control 20261106.38：紧凑双栏控制台，顶部导航、状态和 STOP 同排](docs/images/xeryon-control-20261106-38.jpg)

20261106.38 实际程序截图（未连接设备）。[下载 Windows 诊断版](https://github.com/Baoqi-pku2/xeryon-control/releases/tag/v20261106.38)。

## 运行

Windows 建议使用 Python 3.13（含 Tcl/Tk）。首次运行双击 `run_gui.cmd`，脚本会创建本地环境、安装依赖并打开界面；打开界面不会自动连接设备。

也可在项目根目录执行：

```powershell
py -3.13 -m venv .xeryon-venv
.\.xeryon-venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.xeryon-venv\Scripts\python.exe examples/control_gui.py
```

本版采用紧凑双栏，顶部标题、导航、状态、STOP 同排；默认 1320×820，最小 1100×760。主页面和高级设置不做纵向滚动，日志与结果表保留内部滚动。

连接前核对实际串口、CAN 波特率、节点和编码器型号。源码默认 `slcan / COM3 / 125 kbit/s / Node 32 / ENC_RES_1MU`，主推进器使用厂家默认节点 32。不要将软件显示的默认行程视为已测得的机械端点。配置入口及常见故障见 [开发与调试](docs/DEVELOPMENT.md)。

## 离线测试

从项目根目录运行，不需要 CAN 设备：

```powershell
.\.xeryon-venv\Scripts\python.exe -m unittest discover -s examples/tests -p "test_*.py" -v
.\.xeryon-venv\Scripts\python.exe tools/verify_ui_layout.py --output layout-check.json
.\.xeryon-venv\Scripts\python.exe tools/verify_ui_responsiveness.py --output responsiveness-check.json
```

GUI 测试需要可用的桌面和 Tk。GitHub Actions 配置为 Windows 上执行离线单元测试，不执行硬件诊断或运动。手动脚本 `examples/tests/verify_communication.py` 不属于这组离线测试。

## Windows 打包

完成环境安装后双击 `build_portable.cmd`，输出为 `release/portable/XeryonControl.exe`。也可执行：

```powershell
.\.xeryon-venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.xeryon-venv\Scripts\python.exe -m PyInstaller --noconfirm XeryonControl.spec
```

第二种方式输出到 `dist/XeryonControl.exe`。本地路径、Python 发行版和打包依赖可能影响二进制哈希；重新打包后需重新检查，不能沿用历史 EXE 的验证结论。

## 协作

欢迎通过 [Issues](https://github.com/Baoqi-pku2/xeryon-control/issues) 报告复现过程，通过分支和 Pull Request 提交修改。请先读 [CONTRIBUTING.md](CONTRIBUTING.md)。直接写入仓库需要维护者邀请；公开可见不等于任何人都能直接修改主分支。

| 路径 | 内容 |
| --- | --- |
| `examples/control_gui.py` | GUI 与控制流程 |
| `examples/position_panel.py`, `examples/ui_theme.py` | 紧凑控制面板与界面样式 |
| `examples/common/` | CANopen 参数与底层工具 |
| `examples/tests/test_*.py` | 离线回归测试 |
| `tools/verify_ui_*.py` | 离线布局和响应检查 |
| `tools/diagnose_*.py`, `tools/run_hardware_validation.py` | 设备诊断脚本，运行前必须审阅实际动作和参数 |
| `eds/` | 厂家设备描述 |

`configuration.py`、`homing.py`、节点配置和硬件诊断脚本可能改变设备状态或参数，不是快速入门命令。调试从离线测试开始，涉及真实运动的修改须单独安排实机验证。

## 来源与声明

上游：[Xeryon-Precision/XLA-INTG_prog_examples](https://github.com/Xeryon-Precision/XLA-INTG_prog_examples)。接线及厂家使用说明请查阅上游文档和官方手册。本项目保留 [NOTICE.txt](NOTICE.txt) 及源码中的厂家声明；未将上游代码重新授予 MIT/Apache 等许可证。仓库公开用于查看与协作，不应据此推断获得额外的再许可权利。

本仓库只收录当前源码和协作资料，不包含本地虚拟环境、旧版 EXE、实验原始数据或完整历史诊断记录。当前验证范围见 [版本说明](docs/RELEASE_20261106_38.md)。
