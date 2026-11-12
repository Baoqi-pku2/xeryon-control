# 协作快照验证 · 2026-11-06

在独立导出目录中使用 Windows / Python 3.13 和该快照当时锁定的依赖运行：

```text
python -m unittest discover -s examples/tests -p "test_*.py" -v
Ran 218 tests in 52.791s
OK
```

另使用 `tools/verify_portable.py` 比较本地 20261106.38 EXE 与本快照的十个应用模块，全部匹配。EXE SHA-256 与 [版本说明](RELEASE_20261106_38.md) 一致。应用源码未因导出而修改。

这次测试使用离线合成输入与模拟 I/O，没有执行实机诊断、连接设备或发起真实运动。历史布局/响应检查的范围见版本说明；没有为这次仓库整理重复宣称新的实机验证。

GitHub Actions 配置已提供，远端实际执行结果需查看该提交对应的 Actions 记录。
