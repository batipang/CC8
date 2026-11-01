# F-06 分步职责拆分

第一步只提取已经独立拥有资源的组件，不把 Bridge 的共享可变状态拆成多个无边界的文件。协议、代次、队列容量、线程创建、音频阈值、校准和参考时钟均不改变。

| 模块 | 当前所有权 | 调用/释放边界 |
| --- | --- | --- |
| speech_providers.py（103 行） | ASR 实例、TTS 取消与 provider 自有缓存 | 识别 worker 调 recognize；回答 worker 调 stream / synthesize；既有 cancel 路径取消 provider；不开设备 |
| usb_transport.py（120 行） | 单个 adb 子进程、原始 stdin/stdout 和不留存 stderr 的 drain 线程 | TransportHub/Channel 取得 stream 并负责关闭；USB 检测不打开 Android UI 或采集 |
| reply_types.py（30 行） | 队列工作项类型与 mood 纯函数 | 只传值，不拥有线程/会话/播放状态，不新增持久化 |
| bridge.py | 现有会话/代次、识别和回答 worker、校准/看图线程协调、队列、锁 | 保留现有实现；仅重新导出上述名称，原入口与测试导入兼容 |

本步移动 12 个定义，移动后的 AST 与原定义相同；Bridge / main / run_backend 的定义 AST 也相同。原 bridge.py 的公开 LocalSpeech / DirectSpeech / USBPipe / 队列类型及辅助函数仍通过显式 import 别名导出。provider 的内部全局查找现在位于各自模块；调用者应注入假 provider 或针对所属模块打桩，不依赖未公开的 bridge 内部导入。

Bridge 仍有 2000 余行，本步不关闭 F-06。下一步依次梳理视觉与桌面照片生命周期、播放/字幕时钟、捕获/校准及识别调度、会话事件与任务协调。每一步先明确状态拥有者、不可变请求/代次票据和窄回调边界，再移动实现；不采用把全部 Bridge 传给多个 mixin 继续共享写字段的方式。

现有 guard / dsp_lock / write_lock / status_lock / done 及临界区在本步保持原位，没有新增嵌套获取顺序。后续拆分必须逐条审计锁调用链，再记录允许顺序和资源退出流程；不能仅凭提取文件宣称并发所有权已完成。新模块目标小于 400 行，Bridge 也应随后续步骤收敛。

验证：拆分前后同一 installer 16、host 345（4 私人录音 skip）、tooling 47、Java 15 checks；Ruff / Java formatting / strict mypy 6 模块通过。没有新增或读取音频/图像，不把离线相等性当作声音场景实机验收。

第一步 [PR #18](https://github.com/batipang/CC8/pull/18) 的 [真实 CI](https://github.com/batipang/CC8/actions/runs/41333342689) 八项通过并合入主分支。

## 第二步：视觉生命周期

`VisionController`（249 行）成为请求代次/nonce、设备挑战、CameraInbox 和桌面拍照 waiter 的唯一生产代码写入者。Bridge 保留 request_snapshot / cancel_snapshot / capture_snapshot / begin_visual / answer_visual 入口作为薄转发，协议字段、前台授权、5 秒预览、错误提示和取消结果保持一致。测试只将内部状态定位改到所属组件，原断言、拒绝条件和超时保留。

组件通过 frozen `VisionHooks` 获取明确的会话命令、当前就绪位和发送/回答/指标回调；不传入整个 Bridge，不持有共享 metrics dict。会话 generation、连接、任务和音频仍归 Bridge；视觉组件不能打开麦克风或自己生成拍摄授权。回调通过 facade 动态解析，保留原有替换 provider/测试端口的能力。

视觉锁顺序：共享会话 `guard` → `CameraInbox.lock`；begin/invalidate 的 inbox 操作完成后释放内锁，再发送设备事件。`CameraInbox.feed` 不调用会话回调，wait 的 valid 回调在 inbox 锁之外执行；snapshot 清理也先释放 inbox 锁再取会话 guard。与原实现相同，拍照线程只等待/接收一帧，桌面 waiter 始终在 finally 收尾；旧请求清理依赖 capture 身份，不能清除新请求。设备挑战更新仍由设备事件线程负责，session 失效与 capture 代次检查保留。此处仅覆盖视觉路径，不宣称全局 DSP/播放锁审计已完成。

本步原 host 345 项通过（4 private skip），包含语音/按键看图、桌面拍照、取消/旧请求隔离、断线、final 通知异常释放 waiter；Ruff / Java formatting / 六模块 strict mypy 通过。新视觉模块尚不在六模块 strict 覆盖范围，Hooks 有显式接口类型但不冒充全模块类型验收。没有运行摄像头或调用 Dot；待真实 CI，F-06 整体继续保持未完成。

第二步 [PR #19](https://github.com/batipang/CC8/pull/19) 的 [真实 CI](https://github.com/batipang/CC8/actions/runs/41334082851) 八项通过并合入。

## 第三步：输出与暂停生命周期（候选）

`PlaybackController`（约 320 行）统一拥有 playing、设备完成集合/Condition、字幕发布器与每句 playback_id、近期播报文本，以及可撤销暂停 token / 冷却状态。原 Bridge 的 play / play_stream / speak / pause_for_voice / resume_after_rejection / wait_playback 仍为同签名转发；设备 play_done / 暂停恢复确认也由组件收敛。共享 Event/Answer/Metric callback 类型移入 16 行 session_ports，播放组件不依赖视觉组件。

通过 frozen PlaybackHooks 查询当前会话/提供方、调用取消、发包、指标与 TraceBook，并以三个明确回调操作 DSP 拥有的参考时钟；不传整个 Bridge、共享指标字典或可随意改写的 timeline。会话 guard 覆盖完整取消事务，stop_output 先标记停播，clear_cancelled_pause 在其他会话失效后清暂停时钟，通知仍在原事务位置。设备确认与暂停/恢复的 token、代次拒绝逻辑、200 ms 预缓冲、320 样本分片、4 秒完成等待、20 秒暂停超时及 0.6 秒恢复冷却均保留，未调参。

原 345 项 host 在内部状态路径迁移后通过（4 private skip），保留旧完成 ID、误暂停恢复、播放失败取消、待机/提醒忙闲、参考时钟和正常音量插话的原断言。组件具有显式接口但仍不冒充进入 strict 清单。当前 Bridge 为 1731 行，F-06 远未结项。

锁审计另确认了拆分前已存在的问题：完成等待在持有 done Condition 时进入 pause timeout → session cancel，而外层持有 session guard 的取消又等待 done，形成锁循环。已用合入基线和本次纯拆分两份源码，在隔离合成播放/两个 daemon 线程的确定性屏障下复现；不接设备、不用音频文件。纯重构提交先保持行为，随后独立修复该完成等待路径并加入有进程时限的回归；在修复前不能宣称播放锁顺序已完成验收。
