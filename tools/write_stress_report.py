"""Render the measured campaign outcome, including every failed/not-run case."""
import argparse
import html
import json
import re
from pathlib import Path

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('release',type=Path)
args=parser.parse_args()
release=args.release.resolve(); audit=release/'audit'
decision_path=release/'delivery_decision.json'
stopped_by_user=decision_path.exists() and json.loads(decision_path.read_text(encoding='utf-8')).get('stopped_by_user',False)
campaign=audit/'campaign' if (audit/'campaign').exists() else audit
r=json.loads((campaign/'hardware500_summary.json').read_text(encoding='utf-8'))
counts=r['counts']; passed=counts.get('PASSED',0); failed=counts.get('FAILED',0); pending=counts.get('NOT_RUN',0)
package=json.loads((audit/'package_match_delivery.json').read_text(encoding='utf-8'))
lines=['# 随机位置与随机速度实机压力测试报告'+('（阶段结果，尚未完成）' if pending else ''),'',
       f"冻结计划共 {r['planned_count']} 项：**{passed} 项通过，{failed} 项失败，{pending} 项未执行**。",
       '**未达到不限条件稳定版的验收标准。**' if not r['unrestricted_stable_acceptance'] else '**本次冻结计划的实机验收通过。**',
       '本轮实机已因启用阶段约 23 µm 参考变化而失能断连，剩余测试等待实物零点重新确认。' if pending else '',
       f"原计划实机记录时间：{r['first_command_time']} 至 {r['last_verified_stop_time']}；各测试批次运行累计 {r['total_stage_elapsed_s']/3600:.2f} 小时，批间诊断、修复及辅助定位另计。",
       '', '## 用户要求与实际执行条件','',
       '- 保持设定加速度 **50 µm/s²**，不提高到 50000；每个逻辑目标为连续单段运动，不采用段间停机或分段重试。',
       '- 用户目标 50–12000 µm；速度覆盖 3、5、10、20、50、100、300 µm/s。采用提前冻结的分层随机计划，种子 2026110330：83 次全范围随机转移、412 次局部探测、2 次低速长行程、3 次边界目标。不是 500 次独立均匀全行程抽样。',
       '- 原计划包括 70 批序列及 14 批单次，额外停留为 0、0.2、0.5、1、3、30 秒。零额外停留仍保留目标到位、失能和位置稳定核验。',
       '- COM3，节点 33，固件 ID **9a457a1**，1MU 编码器；沿用用户确认的收回端参考 **−143 计数**。换接、重新定位或掉电后不能盲目复用这个参考。',
       '- 使用真实 GUI、CAN 通信及生产控制代码执行。第 1–117 项使用 .31，第 118 项起使用 .32；不是最终版本独自完成 500 项。两个版本的 EXE 分别与对应源码核对，最终 EXE 内 9 个应用模块匹配，并另做独立目录启动自检。',
       '', '## 压力测试结果','',
       '| 速度 µm/s | 通过 | 失败 | 未执行 |','|---:|---:|---:|---:|']
for speed,outcomes in sorted(r['speed_outcomes'].items(),key=lambda item:float(item[0])):
    lines.append(f"| {speed} | {outcomes.get('PASSED',0)} | {outcomes.get('FAILED',0)} | {outcomes.get('NOT_RUN',0)} |")
lines += ['', '| 控制源码 | 通过 | 失败 |', '|---|---:|---:|']
final_source=package['modules']['control_gui']['source_sha256']
for source,outcomes in r['source_outcomes'].items():
    label='最终 .32' if source==final_source else '此前 .31'
    lines.append(f"| {label} / {source[:12]} | {outcomes.get('PASSED',0)} | {outcomes.get('FAILED',0)} |")
lines += ['', '上述统计保留修改前的失败；没有将诊断或后续重定位覆盖为原项目通过。最终版本自己的实机覆盖量见本表，不能把跨版本总数当作同一版本的全通过验收。']
lines += ['', '| 边界项目 | 目标 µm | 结果 | 停稳用户位置 µm |', '|---:|---:|---|---:|']
for case in r['cases']:
    if case['category']=='endpoint':
        stopped=f"{case['stopped_user_um']:.3f}" if 'stopped_user_um' in case else '—'
        lines.append(f"| {case['index']} | {case['target_user_um']} | {case['outcome']} | {stopped} |")
lines += ['',f"证据一致性检查：{'通过' if r['evidence_consistent'] else '未通过'}。统计由 `audit/hardware500_summary.json` 及每个运动的原始 JSONL 复核，逐项保留目标、实际停止位置、到位位、失能确认和失败原因。逐项结果表：`audit/hardware500_cases.csv`。",
          f"各批次中最长连续通过 {r['longest_stage_passed_count']} 项；故障会中断测试，因此不能说 500 项连续无中断完成。界面测试心跳的最大间隔为 {r['max_ui_tick_gap_s']:.3f} 秒，这是界面调度指标，不是设备安全停机的硬实时保证。",
          f"成功项目中，相对用户请求目标的最大停止误差为 **{r['max_passed_position_error_um']:.3f} µm**；这只反映内部编码器，不能证明外部绝对定位精度、负载端机械平滑度或温升。",
          '', '| 批次 | 结果 | 已到位 | 已提交 | 记录的运动命令 | 运行秒数 | 停止断连确认 |',
          '|---|---|---:|---:|---:|---:|---|']
for stage in r['stages']:
    lines.append(f"| {stage['folder']} | {stage['result']} | {stage['completed']} | {stage['submitted']} | {stage['motion_commands']} | {stage['elapsed_s']:.2f} | {stage['shutdown_completed']} |")
dwells=r['thirty_second_dwells']
lines += ['',f"共记录 {r['sequence_dwells']} 次完整步骤间停留，其中 {len(dwells)} 次设定为 30 秒。"]
if dwells:
    lines.append(f"30 秒停留的实测范围为 {min(x['actual_s'] for x in dwells):.3f}–{max(x['actual_s'] for x in dwells):.3f} 秒；通过 GUI 自动接续，未逐步重新点击开始。")
lines += ['', '失败会终止当批并确认禁用、断连；只有复核后才人工启动剩余编号，不自动重试失败项目，不把失败覆盖为成功。若进行恢复测试起点的辅助运动，其记录单列，不能算作原失败项目通过。',
          '', '### 失败项目','', '| 原编号 | 目标 µm | 速度 µm/s | 停止用户位置 µm | 原因 |','|---:|---:|---:|---:|---|']
for case in r['cases']:
    if case['outcome']=='FAILED':
        reason='; '.join(case['reported_error']) if isinstance(case.get('reported_error'),list) else str(case.get('reported_error',''))
        stopped=f"{case['stopped_user_um']:.3f}" if 'stopped_user_um' in case else '未确认'
        lines.append(f"| {case['index']} | {case['target_user_um']} | {case['speed_um_s']} | {stopped} | {reason.replace('|','/')} |")
lines += ['', '## 已实施的修复及实际效果','',
          '此前从 .27 起的修复也保留在最终程序中：队列每步到位、失能后按设置停留并自动接续；50 µm 边界的浮点误报处理；运动前坐标与量化校验；STOP、故障及失联取消后续步骤；界面事件批量处理与日志容量限制。这些软件机制通过回归和本轮对应实机步骤验证，但不能因此宣称含低速长行程的原工艺全流程已无故障通过。',
          '1. **调整过大的继承 jerk，但越过目标尚未彻底解决。** 实机读到原 jerk 为 992161920 计数/s³。在原加速度 50 下，多种目标发送方式仍越过目标。将 jerk 设为加速度的 10 倍，形成约 0.1 秒加速度过渡，并强制读回校验后，300 µm/s 的小范围往返和 12000→50 µm 连续长行程均通过；但本轮第 95 项 100 µm/s 运动再次触发越过目标保护，不能将早期成功测试解释成全面修复。没有修改目标、提高加速度、放宽到位容差或写入闪存。',
          '2. **目标确认握手。** 保留实时确认和取消/故障检查，取消盲目的固定时间重复触发；本台设备空闲确认位会保持置位，不能强制等待它先清零。',
          '3. **压力测试器的 Windows 进度文件竞争。** 开发中模拟压力在第 492 项前因临时文件替换被拒绝而中止，前 491 项已到位。现保留上一份完整进度文件并在后续保存时恢复；持续锁定和真正磁盘写入错误仍会报错。已增加读锁恢复及写入失败测试。这是测试器问题，不能说成电机运动错误。',
          '4. **压力测试停止位置汇总。** 停止后立即结束测试时，界面缓存可能仍是保护触发时的位置。现将最终位置取自失能后稳定核验记录；旧批次也由独立汇总器从 stop_verified 事件重建，避免误报最终停止位置。最终源码的 500 次模拟回归已验证该修复。',
          '5. **状态检查调度。** .31 每 500 ms 检查一次。直接缩短到 100 ms 的诊断挤占了后续动作的空闲窗口，因此未直接采用。.32 在更短的检查定时器之外，强制每次状态读取后至少空闲 100 ms，并禁止读取重叠。独立诊断两段运动及失能核验通过，随后第 118 项起使用该版本继续原计划。运动参数、到位窗口和停止标准未改变。',
          '', '## 尚未解决：瞬时越过目标和低速长行程','',
          '第 95 项：起点 4296 计数，目标 1886 计数，速度 100 µm/s。实时 SDO 复核位置 1868，触发越过目标保护；随后失能和位置稳定核验为 1885。旧测试进度字段保留停止前读数，最终统计以 stop_verified 原始事件为准。即便最终停止位置接近目标，也不能撤销已经触发的失败。',
          '第 109 项也在 100 µm/s 时越过目标。随后独立诊断将 jerk 从 500 降至 50、保持加速度 50，去程通过而回程仍触发保护；已恢复原 jerk，未采用该试调。记录见 audit/jerk50_after_overshoot/result.json。这说明仅调整 jerk 尚不足以解决当前设备的瞬时越过目标问题。',
          '最终 .32 的第 164 项也在 10 µm/s 时失败：起点 6374、目标 6350、停止 6343 计数。更快的读取调度没有彻底消除末端偏离；不能把此前的连续成功归纳为问题已解决。',
          '瞬时越过目标与低速停滞不能混为一谈：对于前者，现有日志不能排除控制器随后本可收敛、但被当前保守保护提前中止的解释。需要结合厂家规定的动态响应和允许偏离范围复核保护策略；本轮没有靠放宽窗口或撤销越过目标保护取得通过。',
          '5 µm/s、加速度 50、有限 jerk 下，约 3000 µm 单次连续运动仍在移动 **2621 计数**后停滞。第一次从 −93 停在 2528，目标 2883；保持同一最终目标并定期刷新后，第二次从 2834 停在 5455，目标 5810。两次都是 2621 计数，约 2641.71 µm。内部期望位置也停止，目标寄存器仍保留最终目标，错误寄存器没有对应驱动错误。',
          '正式计划再次复现：第 101 项从 1306 停在 3927（正向 2621 计数）；最终 .32 的第 301 项从 10098 停在 7477（反向 2621 计数），原目标仍为 102，内部期望位置也停在 7477，错误寄存器为 0。更快的上位机状态检查没有解决该问题，正反方向及不同起点均复现。',
          '这些读数支持排查控制器轨迹生成/固件或配置限制；尚不能仅凭上位机日志唯一确定固件内部数学错误。固件版本、参数与原始证据已保留，需厂家确认长时轨迹限制以及该版本的连续缓冲接口。',
          '同目标刷新没有解决问题；连续缓冲诊断停在第一个中间目标。这两种实验策略均未进入交付源码。没有用周期性停机、补发失败运动或提高加速度绕过用户要求。',
          '', '## 新出现的坐标校验阻断','',
          '第 321 项结束后，辅助恢复起点在发送计划目标之前失败：启用前原始位置 3708，启用后 3685，变化 23 计数，约 23.18 µm，超过原有 20 µm 参考变化阈值。程序取消目标、撤销坐标校验，并确认失能断连。该辅助失败不属于冻结计划 321 项之内，未被隐去。',
          '后续只读检查确认：位置和内部期望位置均为 3685，控制字 0，状态字 1616，目标寄存器 0，错误寄存器 0。记录：`audit/after_reference_jump_readonly.json`；辅助失败：`audit/campaign/setup_after321/result.json`。当前读数不能自行证明原收回端零点仍有效，因此在用户重新核对实物零点之前，不再发出运动命令，也不能擅自回到 50 µm。',
          '控制字和目标位置共用 RPDO；当前流程未在启用前预装当前位置作为保持目标。已准备 `tools/diagnose_enable_hold.py`，并通过 5 项离线检查（启用顺序、读回不一致、运行状态拒绝、取消和非启用状态）。这是待验证的原因与诊断方案，尚未实机验证，未进入 .32 EXE，不能宣称坐标跳变已修复。',
          '', '## 验证与交付边界','',
          '- 最终生产控制代码回归：193 项通过（37.900 秒），包含状态读取空闲间隔和测试编号选择检查。原始日志：`audit/tests_delivery.txt`。',
          '- 最终源码的软件压力 500/500 通过并由原始轨迹独立复核：`audit/simulated500_delivery/evidence_check.json`。模拟运动加速、停留为零，不能计入实机次数。含无效实验刷新代码的模拟结果另外保存，不能用来证明实机问题已解决。',
          '- 源码和 EXE 启动自检通过；包内代码匹配：`audit/package_match_delivery.json`。',
          f"- EXE SHA-256：`{package['exe_sha256']}`。",
          '- 所有到位误差仍使用原有窗口；未放宽停止保护、坐标校验或失能确认。',
          '', '## 结论','',
          '本轮能交付改进后的控制程序和可追溯测试结果，但上述瞬时越过目标及低速连续长行程问题仍存在，不能称为满足全部原要求的稳定版本，也不能声称“所有问题已修复”。最终状态以原始实机记录及本报告统计为准。']
if r['problems']:
    lines += ['', '### 证据检查未通过项','']+['- '+value for value in r['problems']]
resource_file=campaign/'resource_samples.jsonl'
if resource_file.exists():
    samples=[json.loads(line) for line in resource_file.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    samples=[item for item in samples if item['PrivateMB']>10]
    if samples:
        lines += ['', '## 资源采样','',
                  f"主测试进程的 {len(samples)} 次抽样中，工作集 {min(x['WorkingSetMB'] for x in samples):.2f}–{max(x['WorkingSetMB'] for x in samples):.2f} MB，句柄数 {min(x['Handles'] for x in samples)}–{max(x['Handles'] for x in samples)}。",
                  '这些是分批运行中的抽样，故障后曾重启进程，不能证明长期零泄漏。完整时间、PID 和读数见 `audit/campaign/resource_samples.jsonl`。']
text='\n'.join(lines)+'\n'
if stopped_by_user:
    text=text.replace('本轮实机已因启用阶段约 23 µm 参考变化而失能断连，剩余测试等待实物零点重新确认。',
                      '已按用户“先停止实机测试，交付当前结果”的要求结束本轮实机测试；本次交付截至 321 项的结果，其余 179 项未执行，不会自动续测。')
    text=text.replace('因此在用户重新核对实物零点之前，不再发出运动命令，也不能擅自回到 50 µm。',
                      '本轮现已按用户要求结束；以后若另行开展实机测试，需重新核对实物零点并明确启动，不会自动续测或自动回到 50 µm。')
for before,after in (('最终 .32','当前 .32'),('最终版本','当前候选版本'),
                     ('最终生产控制代码','当前候选生产控制代码'),('最终源码','当前候选源码'),
                     ('最终 EXE','当前候选 EXE')):
    text=text.replace(before,after)
if campaign!=audit:
    for name in ('hardware500_summary.json','hardware500_cases.csv','jerk50_after_overshoot/result.json'):
        text=text.replace('audit/'+name,'audit/campaign/'+name)
(release/'压力测试报告.md').write_text(text,encoding='utf-8')
def inline(value):
    value=html.escape(value)
    value=re.sub(r'\*\*(.+?)\*\*',r'<strong>\1</strong>',value)
    return re.sub(r'`([^`]+)`',r'<code>\1</code>',value)

parts=[]; in_table=False
for line in text.splitlines():
    if line.startswith('|'):
        if not in_table:
            parts.append('<div class="table"><table>'); in_table=True
        if re.fullmatch(r'[| :\-]+',line):
            continue
        parts.append('<tr>'+''.join('<td>'+inline(cell.strip())+'</td>' for cell in line.strip('|').split('|'))+'</tr>')
        continue
    if in_table:
        parts.append('</table></div>'); in_table=False
    if not line.strip():
        continue
    heading=re.match(r'^(#{1,3}) (.*)',line)
    if heading:
        level=len(heading[1]); parts.append(f'<h{level}>'+inline(heading[2])+f'</h{level}>')
    else:
        parts.append('<p>'+inline(line)+'</p>')
if in_table:
    parts.append('</table></div>')
page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>推进器实机压力测试报告</title><style>
body{font:16px/1.75 "Microsoft YaHei",sans-serif;color:#253140;background:#f4f6f8;margin:0}
main{max-width:1100px;margin:32px auto;padding:32px 40px;background:white;border:1px solid #dce2e8}
h1{font-size:28px;line-height:1.4}h2{font-size:22px;margin-top:36px;border-top:1px solid #e1e7ed;padding-top:20px}
h3{font-size:18px}p{margin:12px 0;overflow-wrap:anywhere}strong{color:#a32b24}code{font-size:13px;background:#f0f3f6;padding:2px 4px;overflow-wrap:anywhere}
.table{overflow:auto}table{width:100%;border-collapse:collapse;font-size:14px}td{padding:8px 10px;border:1px solid #dce2e8}tr:first-child{background:#edf2f7;font-weight:bold}
a{color:#245fa0}@media print{body{background:white}main{margin:0;padding:0;border:0}.table{overflow:visible}h2{break-after:avoid}tr{break-inside:avoid}}
</style><main><p><a href="压力测试报告.md">Markdown 报告</a> · <a href="audit/campaign/hardware500_cases.csv">逐项 CSV</a> · <a href="audit/campaign/hardware500_summary.json">完整证据索引</a></p>'''
page+='\n'.join(parts)+'</main></html>'
(release/'压力测试报告.html').write_text(page,encoding='utf-8')
print(str(release/'压力测试报告.md'))
