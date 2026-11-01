# P0 第一批：诊断与 Mac 后台恢复（0.12.0）

Android 保持 0.8.6。本批实现 P0-01 / P0-02 的软件基础，未关闭 TODO 的完整实机验收项。所有命令从**实际连接 CC8 的运行目录**执行，不要从第二个克隆重复启用服务。

## 诊断

```sh
.toolchains/cc8/voice-venv/bin/python cc8-assistant/host/diagnose.py --health
.toolchains/cc8/voice-venv/bin/python cc8-assistant/host/diagnose.py
```

前者实时请求 owner-only Unix IPC，显示初始化、识别/回答线程、连接、收音和播放状态。`model_ready` 表示本地初始化完成，不保证远端接口、额度或权限可用。后者导出 JSON 到忽略的 `device-access/private/diagnostics/`：目录 0700、文件 0600。两条命令均不打开界面、麦克风或摄像头。

内存中最多保留最近 128 个请求的随机 request ID、generation、可取得的 Dot turn ID、来源、阶段、终态及错误类别。记录门控起音、断句结束、ASR、首个可播句、TTS 首 PCM、播放参考回执、暂停/恢复、播放结束和任务结果。不导出音视频、识别文本、回答、URL、异常正文或密钥；重启清空，导出才保存这些非内容指标。

按输入来源和实际工具阶段输出样本数、成功率、p50 / p95。缺失阶段为 null，不补零、不算入该项时延样本。`task_outcome` 独立保存实际工作结果，后续朗读失败不撤销 Dot 已完成工作。统计范围是最近请求窗口，不是服务启动后的全量统计。

所有阶段用 Mac 单调时钟计时。`voice_detected` 是门控检测时刻，不是真实人声起点；`render_ack` 是设备接受 PCM 的回执，**不是扬声器实际起声**。当前 APK 没有新增真实出声采样点，因此相关耗时只能定位瓶颈，不能宣称达到端到端语音指标。固定真人样本与 1/3 米、电视背景等仍须按 [TODO](TODO.md) 验收。

错误按发生阶段及稳定类别记录，只对明确的 HTTP 状态分类，未知供应商错误码不猜测。没有自动重试模型请求、工具写入或旧播报。

## 登录启动与恢复

```sh
# 安装/启用当前用户服务；只启动后台
.toolchains/cc8/voice-venv/bin/python cc8-assistant/host/service.py install
.toolchains/cc8/voice-venv/bin/python cc8-assistant/host/service.py status

# 明确停止并禁用恢复；launch.py --stop 也会禁用监督服务
.toolchains/cc8/voice-venv/bin/python cc8-assistant/host/service.py stop

# 用户明确恢复后重新启用；卸载则移除登录服务
.toolchains/cc8/voice-venv/bin/python cc8-assistant/host/service.py start
.toolchains/cc8/voice-venv/bin/python cc8-assistant/host/service.py uninstall
```

LaunchAgent 位于当前用户 `~/Library/LaunchAgents/`，标签由工作目录摘要区分，仅含本机路径，无凭证。enabled 标记、锁和监督状态留在私有运行目录。明确停止先落盘 disabled，再退出 LaunchAgent；即使遇到拉起竞态，disabled 入口也不会创建后台。

OS 文件锁覆盖模型加载到退出，直接执行 bridge.py 也不能绕过；监督进程另持独占锁，不依赖 PID 文件判断锁归属。子进程失败按 1 / 2 / 4 / 8 / 16 / 30 秒退避，健康满 60 秒后重置；监督进程自身异常由 launchd 限速恢复。初始化允许 180 秒，此后连续健康失败会重建后台。单纯设备断连不会反复启动模型。

新后台 stdout/stderr 的 service.log 每份最多 2 MiB，最多当前加 3 份历史。接管旧手动进程时原 stdout 描述符不变，完整轮转在下一次受监督启动后生效；当前部署采用全新受监督启动。

恢复不打开 CC8、不重播队列、不恢复工具写入或录音。前后台、手动停止、两路全断及重连不自开麦的规则保持。Mac 休眠期间服务不持续工作，登录/睡眠恢复仍需实机观察。

## 验证范围

离线回归覆盖脱敏、缺失样本、代次绑定、已完成工作与朗读失败分离、私有 IPC/文件、十轮真实子进程锁释放、十轮模拟退出退避、明确停止及日志轮转。实际运行目录另外验证一次真实强制退出恢复、明确停止和重新启用，见 [VALIDATION](VALIDATION.md)。

以上不等于完整设备验收。十轮完整服务联调、20 次物理 USB/Wi-Fi 切换、三轮 15 分钟待机及 72 小时观察仍待完成。下一批按 P0-03 验证本地唤醒，再以样本推进 P0-04 / P0-06；本批未调整声学阈值。
