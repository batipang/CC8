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
