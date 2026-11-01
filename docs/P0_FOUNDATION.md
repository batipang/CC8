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

所有阶段用 Mac 单调时钟计时。`voice_detected` 是门控检测时刻，不是真实人声起点；`render_ack` 是设备接受 PCM 的回执，**不是扬声器实际起声**。后续源码候选 0.13.6 / APK 0.8.8 增加了[播放头进度回执](PLAYOUT_RECEIPT.md)，仍没有声学采样点，因此相关耗时只能定位瓶颈，不能宣称达到端到端语音指标。固定真人样本与 1/3 米、电视背景等仍须按 [TODO](TODO.md) 验收。

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


## 0.12.2：登录环境与 Dot 依赖

launchd 不会自动读取交互 shell 的 PATH。服务 plist 仅保存 PATH（不保存环境密钥），Dot 子进程自身也补齐常见本机工具目录；保留已有绝对路径顺序，剔除空项/相对项，再加入 Homebrew 和用户本地工具目录，不修改全局 shell 或模型配置。更新已有服务的启动环境时，先 service.py stop 再 service.py start，确保 launchd 重新加载 plist。

health.dot_runtime 返回 Codex 可发现、包装器是否需要 Node、Node 是否可发现等就绪位。这是本地依赖检查，不代表已完成远端模型认证或请求。缺少 Node 时 npm 安装的 Codex 包装器可能立即以 127 退出，语音识别/TTS 本身仍正常。实际故障及修复验证见 VALIDATION.md 文末。

## 0.12.4 候选：统一配置

F-09 增加了非敏感配置来源、迁移与恢复，见 [CONFIGURATION.md](CONFIGURATION.md)。默认仍只持久化 PATH；安装时明确提供的白名单非敏感环境覆盖会经校验后继承到登录服务，不保存 API key 或其他环境秘密。模型初始化前校验错误字段，避免登录后悄悄换回默认选择。本段是候选软件能力，实际运行环境需另行部署验证。


## 后续软件：分轮诊断采集

`./cc8 measure` 可在不更新运行安装的情况下读取既有 schema 1 IPC，用于 P0-01 基线与后续固定样本/故障注入的指标留存；见 [MEASUREMENT_RUNS.md](MEASUREMENT_RUNS.md)。它不读取麦克风、不交办 Dot 或调用模型，不保存识别/回答/音视频正文。当前软件已在系统 Python 3.9 最小 PATH 对旧运行后台做两次只读轮询，历史 48 项全部排除、新样本为零；这不是新的真人基线，也不宣称设备音频验收。


## 后续软件：真实模拟子进程恢复

`test_supervisor_process.py` 在独立测试进程/临时目录运行真实 Supervisor、Popen、OS worker lock、PID 路径校验、owner Unix health IPC 和终止/回收。模拟子进程只返回固定健康位，不加载模型、打开设备或执行任务。三项覆盖：10 次真实子进程异常退出后的串行恢复；disabled 标记停止活着的进程且不重启；进程存活但连续健康失败时终止替换，之后健康进程按 disabled 停止。均检查无重叠 worker、无遗留进程与最终 stopped。

为快速 CI，wait gate 缩短，保留并断言 1 / 2 / 4 / 8 / 16 / 30 秒退避参数；健康失败案例单独前移 180 秒初始化预算，不把约三秒测试写成真实几分钟恢复计时。全局模型运行所有权检查在该模拟 fixture 中替换，避免接触现有后台；其真实锁测试仍由 F-15 的独立回归负责。未调用 launchctl，未安装/启停真实 LaunchAgent，未改当前服务或麦克风。实际登录、睡眠和十轮完整生产服务验收仍开放。


## 0.13.0 候选：云识别之前本地唤醒

双 KWS、等待/有界窗口、同句话语缓冲及许可/资源/合成检查见 [LOCAL_WAKE.md](LOCAL_WAKE.md)。等待期间不交办云 ASR，缺少模型不走文本唤醒降级；15 分钟黑屏仍用本地名字亮屏。已通过离线与真实模型合成检查，尚未部署或完成真人/物理验收。
