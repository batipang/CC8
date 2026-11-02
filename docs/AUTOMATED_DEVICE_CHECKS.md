# 自动设备检查

2026-11-02 用户要求减少重复人工操作，改用自动测试。`cc8 verify-device` 从 Mac 检查已有安装，不安装 APK、不重启后台、不增加设备端调试接口；默认只输出计划，离线 CI 不接触设备。

```sh
# 只看计划：不查设备/模型，不创建目录，不开麦
./cc8 verify-device --runtime-root /absolute/live/workspace
# 实机：固定播报、播放回执、字幕布局和逻辑切换
./cc8 verify-device --runtime-root /absolute/live/workspace --execute
# 明确启用前台开麦、Home 停采、维护重开保持关闭
./cc8 verify-device --runtime-root /absolute/live/workspace --execute --capture
# 追加真实 15 分钟待机，黑屏后由 Mac 播放合成问句
./cc8 verify-device --runtime-root /absolute/live/workspace --execute --capture --standby --acoustic
```

runtime-root 指**实际运行目录**。现场候选为 `/Volumes/AI/Codex/CC8-P0`，冻结源码 p0-validation-base-20261102.1：后台 0.13.7 / APK 0.8.8 code 33；main 的 P1 软件保持独立。深层候选目录曾超出 macOS Unix socket 路径限制，已改为短路径并重新创建独立 venv。

## 门控与动作

- 唯一正常 Android USB AILABS_S6 / SDK 27 / shell UID 2000，原装电源连接。每次 Android 操作重核原 USB transport，不跟随被更换的目标。
- 已有 owner-private IPC，实际后台版本与指定源码声明一致；USB、已认证 Wi-Fi 都就绪。助手须在前台，麦克风、播放、校准、相机均空闲；拒绝接管正在使用的设备。
- execute 会通过现有 MiniMax TTS 播放固定公开文字。capture 明确启用**相关语音测试**，仅通过既有可见 MainActivity 和 auto_start_voice=true 开始；维护打开仍为 false，不增加远程开麦 IPC。
- standby 要求已选唤醒模式，真实等待约 900 秒，临时防止 Mac 空闲睡眠；过早黑屏、连接/采集丢失、超时均不算通过，没有修改待机阈值。
- acoustic 仅在黑屏且采集仍开启时，用 Mac 现有 Tingting 音色说一次“大黄蜂，一加一等于几？”。不调整音量；静音、距离、方向或噪声可能导致未确认。不要求用户代读，不自动重试。

## 证据与界限

| 检查 | 实测依据 | 不据此宣称 |
| --- | --- | --- |
| 维护打开不开麦 | Mac health 与设备 snapshot 双端关闭 | 全部竞态均已验收 |
| 播报与字幕 | 新的 completed announcement、playout_ack、playback_done；播放期间当前行可见、字幕/进度及光环/状态无重叠 | 精确声学起声/延迟、字体审美 |
| 逻辑切换 | 抑制 USB 12 秒，Wi-Fi 接管、USB 优先返回，未自行开麦 | 真实拔线；不计入 20 次物理往返 |
| 采集生命周期 | 明确打开启动并完成校准；Home 后 inactive 且帧计数不增长；维护重开仍关闭 | 全部断网、登录/睡眠和故障矩阵 |
| 待机 | 新采集后的真实等待、黑屏，前台及采集仍有效 | 三轮全部完成或 72 小时稳定性 |
| 合成声学唤醒 | Mac 扬声器→CC8 麦克风→本地唤醒→新 voice 回合有 ASR/大脑/播完回执；设备亮屏且前台 | 回答语义、真人中英召回率、1/3 米准确率、真人打断延迟 |

旧版 0.12.2 / APK 0.8.6 的人工观察独立保留：基础对话、普通音量插话、单次物理往返、Home 重开和一次黑屏唤醒。半秒内是用户体感；拔 USB 约 9 秒连接空窗为采样观察，之后用户手动开麦。新版自动结果不能改写旧版证据的版本、样本量或性质。

## 报告与恢复

执行源码目录的 device-access/private/device-checks/ 保存逐项原子检查点，目录 0700 / 文件 0600。仅保存测试名、状态、固定错误代码、版本、自动脚本开始时的 SHA-256 与单端耗时，不保存 dumpsys 全文、设备身份、识别/回答正文、录音、图像或凭据。开发中较早启动的报告不自动获得后续脚本摘要。

同一实际运行目录共享排他测试锁。后台实例改变即中止；已触及 UI 的路径在失败、超时或 Ctrl+C 后均尝试 Home 停采，再维护打开恢复前台，核对两端麦克风及相机关闭。无法确认则 cleanup_unconfirmed，不能给出通过结论。启动前拒绝不改变原状态；SIGKILL 或断电等无法运行 finally 的情况不保证进程外清理。

报告固定 physical_usb_unplug_tested=false、human_accuracy_asserted=false、acoustic_latency_asserted=false、full_p0_acceptance=false。离线 fixture 验证门控、计时、旧回执排除、失败清理和脱敏；实机结果另记 VALIDATION。一轮自动通过不关闭整个 P0。


## 无录音的英文唤醒回归

```sh
# 默认只打印计划，普通 Python 可运行，不载入模型或创建文件
./cc8 verify-wake --model-root /absolute/live/workspace
# 用已准备的模型环境执行；模型缓存必须先通过既有哈希校验
/path/to/evaluation/python cc8 verify-wake --model-root /absolute/live/workspace --execute
```

这项检查只生成 Mac 公开合成语音，不播放声音、不读取麦克风、不联系云端、不操作 CC8，也不修改运行环境。执行源码目录的 `local_wake.py` 是被测实现；model-root 仅指定校验过的模型缓存，不代表自动改测该目录的源码。

固定语料含 12 个正样本（Samantha、Alex、Daniel、Tingting，各三档语速）及 10 个普通/近音负样本（含 Bumble、Bubble bee）。逐个以 20ms 帧馈送，覆盖即时、60/140/160/480ms 前导和 1s 连续上下文；另保留无初始化的两组参照。六组 production 条件全部正样本命中且全部负样本不触发才通过；少样本、生成失败、取消或条件缺失均不能通过。参照条件只作观察，不参与放宽标准。

仅在私有 wake-checks 目录保留固定样本 ID、标签、布尔结果、源码/语料/合成 PCM 摘要、原生库版本和二进制摘要；临时合成音自动删除。失败退出 2，Ctrl+C 退出 130 并在已开始评测时写 cancelled；普通计划退出 0 不代表通过。合成语音覆盖只能定位回归，不能替代真人、声学距离、电视背景噪声或中英文实际召回率。

2026-11-03 隔离研究发现原生 KWS 在连续静音后的重置边界也存在漏检；上游已有 [issue 3990](https://github.com/k2-fsa/sherpa-onnx/issues/3990) 和 [待合入修复 3994](https://github.com/k2-fsa/sherpa-onnx/pull/3994)。以固定 v2.0.1 源码隔离回编，官方复现从 59/65 改为 65/65，但自定义 Bumblebee 语料仍有漏检；扩大解码路径还出现近音误触发。320ms 初始化也未覆盖全部时点。因此未合入这些运行行为改动、未把实验原生库安装到正在运行的助手中，P0-03 保持开放。
