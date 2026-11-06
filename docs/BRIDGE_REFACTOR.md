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

### 独立锁循环修复候选

纯重构提交 `6f01187` 之后单独修复：完成循环先释放 done Condition 再调用 pause wait / cancel，重取 Condition 后检查 ACK，避免丢通知；暂停到期取得会话 guard 后再次核对 generation，已失效则直接退出，不取消新请求。4 秒/20 秒等待预算和音频参数不变。

输出路径允许顺序为 session guard → completion Condition；完成循环不会持 Condition 调暂停、valid 或取消回调。设备 ACK 的 generation 回调是明确的无锁只读取值，不能未经审计改成获取会话 guard。字幕 iterator / publish 回调仍在 finally 释放；收到设备 app 的 play_done 仍不是物理扬声器出声时间的证明。

新增 `test_playback_lock.py` 在无设备的子进程内，用两个真实线程和 pause 映射的 started 读取屏障控制并发：修复前 6.2 秒有界失败并报告锁循环，修复后约 0.17 秒通过，同时断言旧到期不会再次推进/取消新 generation。子进程最长 12 秒，异常和活线程都使测试失败，不掩盖后台线程异常。整套离线 installer 16、host 346（4 private skip）、tooling 47、Java 15 checks 通过，质量检查通过。后台候选独立升为 0.12.6 / foundation-20261102.2；未部署，待 CI。

第三步的纯重构与独立修复已由 [PR #20](https://github.com/batipang/CC8/pull/20) 合入，[真实 CI](https://github.com/batipang/CC8/actions/runs/41336074661) 八项通过，包含 Linux/macOS 的新子进程死锁回归。

## 第四步：包、参考与断句三层（候选）

| 模块 | 所有权 | 明确边界 |
| --- | --- | --- |
| CapturePipeline（103 行） | 有界麦克风包 lookahead、序号与设备纳秒时间戳调度 | USB 80 ms / Wi-Fi 120 ms、64 包上限和非单调拒绝保留；只接收现有会话包，不打开设备 |
| EchoPipeline（140 行） | RenderTimeline、参考偏移、校准样本、native AEC / stop AEC 和 DSP 锁 | 创建/关闭/预热经既有 factory，暂停时钟以方法端口操作；只有获准前台采样进入 RAM 校准窗口 |
| EndpointPipeline（180 行） | VAD、near-speech、参考片段、local stop 与当前话语证据 | 接受 frozen CleanFrame 字段，不持有 Bridge / metrics dict /识别队列；只向既有排队入口发送同样七字段话语 |

Bridge 仍协调会话、校准提示流程、识别/回答 worker 和任务；其 process_microphone / apply_fast_stop / align_reference / on_packet 入口保留，事件先经过原协议验证。内部原字段改由所属组件写入，测试只移动设置/断言路径，校准取消与旧会话隔离断言保留。纯回声帧不落盘，包的接收不授权开启采集。真实本地唤醒仍是 P0-03，当前只是保留原 text-based wake 行为。

锁顺序保持 session guard → DSP lock；Echo.process 仅在 DSP 锁内调用 native process，释放后才进入 Endpoint / pause / session cancel。包事件线程写 VAD/断句状态，校准/停止通过所属对象方法重置，未新增锁嵌套或修改估计参考时移、噪声阈值、断句和控制延迟参数。播放的参考时钟回调动态指向 EchoPipeline，不能写一份副本造成时钟分叉。

同一组 64 对合成 render/mic 包在基线 `12c218c` 和新源码回放：7 份话语音频、参考音频的内容摘要与证据完全一致，计数同为 mic 8960 / render 10240 samples、0 gap、7 utterances；暂停推断和诊断使用固定合成输入，不读用户录音。原全套 installer 16、host 346（4 private skip）、tooling 47、Java 15 checks 与质量检查通过。新三个动态/native 模块未计入现有 strict 六模块范围。Bridge 仍为 1547 行，本项保持未完成；识别、任务/回答、校准与会话职责继续推进，待 CI。

第四步 [PR #21](https://github.com/batipang/CC8/pull/21) 的 [真实 CI](https://github.com/batipang/CC8/actions/runs/41338562794) 已全部通过并合入。首次 quality job 在下载 pinned actionlint 时遇到 HTTP 504，未执行到代码检查；仅重跑失败任务后通过，未改版本、哈希或检查规则。

## 第五步：识别队列与 worker（候选）

`RecognitionWorker`（289 行）统一拥有容量 2 的识别队列、dequeue/drop/discard、busy 状态及识别线程。Bridge 的 enqueue_recognition / discard_recognition / work 仍为薄转发；health、待机、提醒和自检仅查询其就绪位/队列状态。worker 在整个宿主进程期运行，不自行开启采集、创建 provider 或启动第二份后台。

通过 frozen RecognitionHooks 获取 provider/播放只读查询与会话、指标、视觉/回复提交命令；不传入 Bridge 或整个 metrics 字典。原七字段现场话语和旧离线回放的兼容路径、旧 generation 取消、队列溢出诊断、参考文本/参考 ASR 比较、停止指令、误暂停恢复及异常后继续处理均保留。明确 armed 的回声自检文本对仍在原聚合器中有界记录，不改成默认保存正常对话。当前仍为 legacy 文本唤醒规则，不宣称已实现本地 KWS。

原 host 347 项通过（4 private skip），包括两轮识别间的恢复通知断线不杀死 worker、溢出请求回执、回声拒绝/保留人声、待机/提醒 busy 门槛。测试只移动内部队列/线程/状态定位，溢出用例改为冻结线程启动后构建实际 façade，原断言保留。Ruff / Java formatting / strict 六模块通过；识别组件本身未计入 strict 清单。Bridge 1387 行，回答/任务、校准/自检与会话仍待拆，F-06 未结项，待 CI。

同一组件 start 以独立启动锁做 one-shot 检查；两个并发启动请求的回归确认只创建一个消费者，另一个明确拒绝，不触发 provider。此保护只约束新拆出的组件生命周期，不自动重建或重放队列。


## 第六步：回复队列、对话历史与任务回执（候选）

`ResponseWorker`（232 行）拥有容量 2 的回复队列、busy、单消费者线程与最多 8 条 RAM 对话历史；通过 frozen ResponseHooks 查询会话、provider 和 Workspace，发送输出/诊断命令，不持有整个 Bridge 或共享指标字典。Bridge 的 enqueue_response / respond_work / respond_job / answer 保留同签名入口。显式播报仍通过 offer 在队列满时拒绝，普通回复原有挤出策略和 Announcement waiter 释放不变。

任务与阶段状态仍由 Workspace 统一保存；真实 Dot 回执先完成任务，后续声音停止、设备断开或通知失败不能降级已完成任务。回复 iterator 的 finally 关闭、text_done 的代次匹配、回答/清理异常后继续处理下一个请求，以及视觉与原文播报的分流保持。历史只在原先的有效完成条件下追加，既不落盘，也不增加 provider 重试。

新增与识别相同的并发启动保护覆盖：两次 start 只创建一个回复消费者，另一请求明确失败。原断线恢复、已完成任务不被停止播报降级、任务/提醒忙闲、旧代次和视觉回归原断言保留。installer 16、host 348（4 private skip）、tooling 47、Java 15 checks 与质量检查通过；strict 仍只覆盖原六模块，不把动态 provider 接口计入。Bridge 1255 行，F-06 继续推进校准/自检与会话职责；未调用设备、云服务或私人音频，未替换运行中的助手，待真实 CI。


### 独立修复：自检重试播放状态

审计发现第三步拆分后，run_backend 的 SIGUSR1 自检重试仍读已移除的 bridge.playing。新增回归拦截实际 signal 注册，用真实 Bridge / 合成事件循环且禁用线程/provider/设备，在播放中和有效空闲会话两条路径复现 AttributeError。独立修复为查询 playback.playing；同一回归同时确认麦克风已停、校准中、自检中或播放中均不派发线程、不推进代次，只有原先已 active 的空闲会话重试校准，不授权新采集。后台候选仍属未发布的 0.12.6；运行中版本未动。


第六步与独立重试修复已由 [PR #23](https://github.com/batipang/CC8/pull/23) 合入，[真实 CI](https://github.com/batipang/CC8/actions/runs/41341727093) 八项通过。修复后的全量 host 为 349（4 private skip）。第五步 [PR #22](https://github.com/batipang/CC8/pull/22) 的 [CI](https://github.com/batipang/CC8/actions/runs/41341068368) 同样全部通过。

## 第七步：开场校准和回声自检（候选）

`CalibrationController`（193 行）拥有校准 active 和显式自检 pending / running / failed，只通过 frozen Hooks 查询会话/识别忙闲与调用参考时钟、播放、诊断命令。构造不派发线程。Bridge 保留 startup_reference / calibrate / check_echo 入口；前台开始、Stop 和已获准的自检重试通过 begin / stop / prepare_retry 修改状态。自检局部报告仍经原指标聚合入口发布，识别拒绝/有界文本对的计数位置不变；不传原始 metrics 或 DSP 对象。

唯一开场“汽车人，变形！”、1～6 秒 int16 有声校验、真正开播后才清采样、原 6.4 秒窗口、时移对齐、20 秒安静等待和最多 20 秒识别排空全部保留。旧代次 finally 不能清新会话 active 的回归保留。新增四项合成时钟流程测试覆盖通过、残留候选失败、识别未排空、取消/旧代次及输出异常，确认 running 总是释放且不新开麦克风。原内部状态测试只迁移拥有者路径，摄像头/播报校准忙时拒绝的断言保留。

全套 installer 16、host 353（4 private skip）、tooling 47、Java 15 checks 与 Ruff / Java / strict 六模块通过；新动态校准模块未计入 strict 清单。Bridge 1159 行，仍待会话/设备事件、提醒与依赖装配职责收敛，F-06 未完成。未调用设备、云服务、私人音频或重载当前助手，待 CI。


### 第七步附属纯重构：集中依赖装配

将六组件的依赖绑定移入 236 行 composition.py；它是唯一可读取完整 Bridge 的装配根，只创建 passive 对象并返回 frozen Components，不写会话字段、不启动线程或提供方。Bridge 发布全部对象后才启动识别与回复消费者。各实际组件依旧只持有 frozen narrow Hooks；没有把 Bridge 作为混入对象供业务模块任意修改。Echo factory 显式注入，保持原 façade 的测试/提供方替换入口；动态查询仍指向唯一会话和真实所属组件。

六份构造表达式经显式 session→self / factory / wake-pattern 名称归一后逐项 AST 完全相同；原全套 installer 16、host 353（4 private skip）、tooling 47、Java 15 checks 与质量再次通过。Bridge 缩至 970 行，装配移动与校准移动保持不同纯重构提交；仍不宣称 F-06 已完成。


## 第八步：显式播报与提醒调度（候选）

AnnouncementController 拥有播报 generation 与完成回执，ReminderScheduler 拥有 5 秒 poll 与当前版本化提醒 job。Bridge 的 announce / read_announcement / reminder_action / reminder_tick 保留转发入口；会话取消通过 invalidate 失效播报，valid / Playback 只查询唯一所属状态。两组件通过 frozen Hooks 接收会话锁、明确忙闲判定与输出/Workspace 方法，不持有 Bridge、音频队列或共享指标字典。

原文每段最多 120 字、不经 LLM 改写、队列满显式拒绝、无论状态写入成败总释放 waiter、与麦克风状态独立均保持。提醒同原 guard 事务内检查连接及所有 busy 标志，只在有到期事项时触摸屏幕/提交播报；重复/过期回执、编辑提醒后旧 revision 隔离及存储失败回滚仍由原 Workspace 验证，不启动捕获、不后台打开界面。

原全套 installer 16、host 353（4 private skip）、tooling 47、Java 15 checks 和质量通过；原断言只移动内部状态路径。Bridge 876 行，装配 280 行。严格六模块范围不变，未接设备/提供方或替换运行版本；F-06 与实机 P0 均开放，待 CI。


### 第八步附属：设备控制回执

75 行 DeviceController 统一拥有单调请求序号、单槽 waiter 与回执字段白名单；Bridge 保留 control_device，同一验证过的设备事件委托 receive。只有 status / volume / stop / wake_screen，原 5 秒等待和 finally 清理不变，无开麦/拍照入口。会话 guard 只保护注册/取出/删除 waiter，不在等待远端回执时持有。

新增三项真实调用线程/虚拟通道回归：两个并发音量请求倒序确认仍正确路由，未知 ID 忽略且无关字段过滤；发送断线或等待超时均删除 waiter；非法动作/无连接在发包前拒绝，不开启捕获。未使用真实等待 5 秒、提供方或设备。installer 16、host 356（4 private skip）、tooling 47、Java 15 checks 与质量通过。Bridge 829 行、装配 299 行，新动态模块不计入 strict 六模块清单。

第七步校准与装配已通过 [PR #24](https://github.com/batipang/CC8/pull/24) 的 [真实 CI](https://github.com/batipang/CC8/actions/runs/41342127824) 八项检查并合入。


## 第九步：后台进程运行入口（候选）

191 行 app_runtime.py 收拢 parser、provider factory 装配、TransportHub / owner IPC、信号与主事件循环；Bridge 的 main / run_backend 保留入口，运行锁仍覆盖整个模型加载/服务期。frozen BackendDependencies 在每次调用时从原入口绑定当前 factory / 路径 / 描述，保留现有测试替换点，不静态缓存原提供方。原函数体仅将工厂、私有目录和 parser 描述改为显式依赖，AST 归一比较完全相同；没有改变退出顺序、重试或资源权限。

原公开 USB 工具/信号测试 patch 点仍通过显式模块 re-export 保留；实际 signal 注册、配置错误在 provider 前失败、最小 PATH 和 USB 排除网络 ADB/多设备回归仍通过。全套 installer 16、host 356（4 private skip）、tooling 47、Java 15 checks 与质量通过。Bridge 699 行，仍包含会话与设备事件所有权，不能只因文件变短关闭 F-06。严格检查范围仍六模块，未运行后台、设备或私人音频，待 CI。


第八步已由 [PR #25](https://github.com/batipang/CC8/pull/25) 合入，[真实 CI](https://github.com/batipang/CC8/actions/runs/41342487933) 八项通过。


## 第十步：会话代次与前台生命周期（候选）

329 行 SessionController 唯一拥有 active / epoch / generation / wake / awake、连接选择、rebind 去重、文字请求代次、待机时间与会话/写锁；Bridge 不保留另一份可写会话副本。开始/停止、设备 ready / settings / rebind、手动中断与切换转为会话命令；组件取消、重置/关闭和任务回执经明确接口协调。Bridge 保留同签名外部方法，以及 owner IPC 用到的只读 generation / guard 兼容面。

frozen SessionHooks 接收真实所属组件的 API / 只读 busy 查询与聚合器命令，不接完整 Bridge 或 metrics dict；只有 composition.py 绑定完整宿主。所有会话字段写入收敛在所属模块，进程退出只调用 deactivate / unbind，ACK 取 generation 继续无锁，取消仍是 guard → 组件锁；没有新增锁嵌套、provider 重试或麦克风授权。校准线程仍动态取原 façade 方法，保留替换点和旧代次隔离。

同一组合成操作在合入基线 830454e 与候选运行：16 份会话快照、12 条控制事件及全部指标完全相同，覆盖双 Start 去重、rebind、旧 epoch Stop、15 分钟条件模拟、唤醒屏幕、USB/Wi-Fi 切换、全丢失后恢复不自开麦及文字请求取消。此处是模拟推进时间，不冒充真实 15 分钟待机或物理切换。原全套 installer 16、host 356（4 private skip）、tooling 47、Java 15 checks 与质量通过；测试仅迁移内部状态路径。Bridge 501 行、composition 330 行，状态报告等聚合职责继续收敛；新动态模块仍未计入 strict 六模块清单。无部署/设备/云服务/私人录音，F-06 未结束，待 CI。

第九步后台入口已由 [PR #26](https://github.com/batipang/CC8/pull/26) 合入，[真实 CI](https://github.com/batipang/CC8/actions/runs/41342730902) 八项通过。


### 第十步附属纯重构：只读状态报告

163 行 StatusReporter 拥有状态写入锁、原进程起点、白名单健康字段和错误输出；SessionController 提供 frozen SessionView，只读获取会话标量，不取得 guard 或改变状态。各组件查询仍来自实际所有者，聚合指标以 dict 副本提供；原私有状态文件字段、目录模式、临时文件替换和错误正文脱敏保持，未增加正常聊天持久化或音频保存。原名称/文字唤醒拼写常量集中在 assistant_identity，名字与匹配规则原样保留；不能据此宣称专用本地唤醒已实现。

在同一固定时钟、虚拟 provider、临时私有目录下，已提交会话拆分基线与候选的 health 18 字段、status 48 字段及脱敏错误 log 完全相同；合成识别文本不会进入 health，异常正文 sentinel 不会进入 log。全套 installer 16、host 356（4 private skip）、tooling 47、Java 15 checks 和质量通过。Bridge 403 行仅保留 façade / 已验证事件分派 / 指标聚合；SessionController 349、composition 363，其他新职责模块均少于 400 行。严格范围仍六模块，不能把声明的接口等同全模块 strict。

F-06 的软件所有权拆分已收敛，待真实 CI；同样本 native / 声音与实机回归仍留待后续受控验收，本项复选框保持开放。下一步可以独立推进 P0 诊断、恢复及本地唤醒的软件工具；不得用上述合成对照替代真实声音、物理拔插或 15 分钟待机。


第十步会话与报告已由 [PR #27](https://github.com/batipang/CC8/pull/27) 合入，[真实 CI](https://github.com/batipang/CC8/actions/runs/41343729276) 八项通过。F-06 软件分步合入完成，保留声音同样本 / 实机验收缺口，不以纯离线结果勾选整项。

## 原生音频同样本对照（2026-11-04）

分别从第四步前 `12c218c`、第四步完成 `3ef03d5`、第十步收尾 `4c5595f` 导出源码，以相同的 Mac arm64 Python 3.13.17、NumPy 2.3.4、WebRTC VAD 和 pywebrtc-audio 0.2.0 原生扩展运行。扩展核对本机安装记录后复制到隔离目录，SHA256 为 `02381684d2ea29cbe9d42745d03e4f441356f3fb6aa3f82e57220aa1914a41b8`。三条输入来自先前公开文字的系统合成语音，逐条核对原 PCM 摘要，再生成固定回声延迟、衰减和近端混音。

| 每个版本的对照 | 组数 | 核对结果 |
| --- | ---: | --- |
| 纯回声、近端、重叠语音，各用 10/20 ms 分包 | 6 | 每路原生 AEC3 处理 3,552 帧。参考、输出、成句摘要及计数逐项一致。两种分包下，纯回声均无成句，重叠语音均为 2 次成句、1 次暂停请求。 |
| 30/60 ms 合成回声延迟，各用 10/20 ms 分包 | 4 | 参考偏移分别为 160/640 个采样。每组的 292 帧校准缓存和两路原生滤波预热输出一致。 |
| 暂停恢复及 USB → Wi-Fi → 全断 → USB，各用 10/20 ms 分包 | 2 | 暂停时参考为零，恢复后参考回到指定 PCM。USB 段各处理 242 帧，Wi-Fi 段各 188 帧。旧代次参考和回执被丢弃，全断后两路 AEC 关闭，重连后仍停采。 |

对照走历史版本的普通封包与事件入口，使用内存通道和固定设备时间戳。三份源码的 12 组结果逐字节一致。私有终审 `f06-native-parity-20261104/completed-run/audit.json` 记录输入、脚本、原生库、版本及三套报告摘要，SHA256 为 `0730d09521ea0da0ce50af16f06765835fdd50e4dd0ce05201d0602ee0148554`。这部分覆盖第四至第十步的音频流水线及切换边界。设备声学、真人 ASR、物理拔插仍各按对应 P0 验收。

## 第十一步：设备事件分派

把输入事件的校验与分派收敛到 `DeviceEventRouter`。Bridge 保留 `on_packet` 和 `on_event` 入口，以及非事件音频包的原处理路径。路由器只持有固定的组件引用与命令回调。会话代次仍由 SessionController 持有，录音、播放回执、视觉和任务各自维护状态。路由器没有新增锁或可变状态，原调用顺序及异常处理保持。

对照基线 `05a00a6`，迁移后的事件校验和分派函数语法树完全一致。使用真实包编码、解码和内存 USB 通道重放 18 个固定输入，9 个输出包、18 份状态快照和 1 次非法字段拒绝逐项一致。拆分前后各跑同一组 158 项回归，覆盖设备连接、播放回执、插话、视觉、任务和工作区。Bridge 从 473 行降至 384 行，路由器为 134 行。这次对照没有操作 CC8，实体回归仍按对应 P0 验收。

## 第十二步：插话暂停与恢复

`PlaybackPause` 独立维护暂停令牌、待确认回执、恢复冷却及暂停超时。PlaybackController 仍负责 PCM 输出、句级播放完成和回执等待条件，保留原来的暂停调用入口。两者继续使用同一个播放发布锁和完成条件，暂停准入顺序仍为会话锁再到播放发布锁。超时取消发生在完成条件锁之外。设备确认的暂停和恢复仍由原回调更新回声参考时间线。

拆分前后各运行同一组 110 项定向回归，覆盖超时与新请求并发、暂停回执乱序、误触发恢复、旧回执隔离和播放完成。回执准入、拒绝后恢复和等待函数的语法树一致。另用同一批公开合成 PCM 重放 12 组原生 AEC3 场景，回声、校准、暂停恢复和逻辑换线报告逐字节一致。PlaybackController 从 568 行降至 434 行，PlaybackPause 为 197 行。本轮保持协议、音频参数和运行中的助手版本，实体设备同样本验收仍开放。

## 第十三步：声学探针调度

`AcousticProbes` 集中处理既有录音下的播报、应答及已知波形探针。Bridge 保留原九个调用入口，CapturePipeline 仍持有探针与 PCM，会话代次和锁仍归 SessionController。样本读取、相关分析和 FFT 继续在会话锁外运行，返回时按原规则复核所有者、代次和收音状态。探针只观察已经显式打开的前台录音。

Bridge 从 495 行降至 383 行，AcousticProbes 为 176 行。九个迁移方法在还原 Bridge 的 `guard` 和 `generation` 属性映射后语法树一致。对照 `d0b2b25`，迁移前后各跑 157 项定向回归，均为 153 项通过、4 项私有录音按原规则跳过。12 组公开合成 PCM 的原生 AEC3 回放、校准和路由转换报告逐字节一致。该步保持运行版本、协议和声学参数，实机同样本验收仍开放。

## 第十四步：设备会话协商

`device_session` 接手设备握手、现有录音重绑和设备设置分派，保留 SessionController 的原有事件入口。会话控制器继续持有前台录音、代次、待发屏幕状态和锁。握手仍先更新对端能力和播报中断提示，再在会话锁内发送待发屏幕事件。新模块没有自己的会话状态。会话快照和装配所用的回调类型移入 `session_ports`。

SessionController 从 499 行降至 359 行。对照 `c261830`，三个迁移函数、22 个保留方法及两个数据类型均通过结构对照。拆分前后各有 144 项定向回归通过，4 项私有录音按规则跳过。12 组同一批公开合成 PCM 的原生 AEC3 回放、校准和切换报告逐字节一致。此次为源码拆分，实体设备同样本回归仍开放。

## 第十五步：传输资源归属

把配对身份、证书和发现认证放进 `transport_pairing`，把单条连接的读线程、锁、写入和资源关闭放进 `transport_channel`。`TransportHub` 保留监听、选路及 USB 优先规则，原入口继续提供 `Pairing` 和 `Channel`。配对文件位置、TLS 校验、超时、关闭顺序和线路协议保留原实现。

`transport.py` 从 563 行降至 311 行。九个原有类和函数的语法树完全一致，拆分出来的两个模块都纳入严格类型检查。迁移前后各 82 项定向回归通过，包括本地 TLS、USB 管道、替换连接和日志断管。12 组同样本原生 AEC3 回放、校准及切换报告逐字节一致。实机硬件换线仍按 P0-05 验收。

## 第十六步：收音与应答装配

`composition_audio` 集中装配收音、回声、断句、识别和应答组件。装配仍按原顺序构造组件，延迟解析的回调继续指向原会话和资源所有者。Bridge 仍在启动工作线程前发布完整组件集合。

`composition.py` 从 508 行降至 388 行。展开提取的三个构造后，四个原有装配函数的语法树一致。拆分前后各 110 项定向测试通过，4 项私有录音按规则跳过。12 组同样本原生 AEC3 回放、校准及切换报告逐字节一致。该步是源码装配拆分，实机同样本回归仍开放。

## 第十七步：设备播放回执

将起播、播完、暂停恢复和播放失败回执的接收逻辑移到 `playback_device_receipt`。PlaybackController 保留原入口、播放状态、发布锁和完成条件。设备回执继续按代次和句 ID 核对，锁顺序及通知时点与原实现一致。

`playback_controller.py` 从 442 行降至 358 行。迁移的处理函数和保留的 22 个方法均通过结构对照。拆分前后各 98 项定向测试通过，12 组原生 AEC3 回放、校准和路由切换报告逐字节一致。此次为源码拆分，实机同样本回归仍开放。

## 第十八步：设备录音启停事件

`device_session` 在握手和重绑之外统一处理设备录音开始与停止事件。`SessionController` 保留原入口，以及录音代次、已消费的录音 ID、选路状态和锁。原有确认补发、重复启动拒绝、初始化失败清理和校准线程启动顺序保持一致。

`session_controller.py` 从 598 行降至 501 行。两个迁移函数、31 个保留方法和三个既有协议处理函数均通过结构对照。拆分前后各 112 项定向测试通过，12 组原生 AEC3 回放、校准与路由切换报告逐字节一致。实机同样本回归仍单独验收。

## 第十九步：固定工具题准入

`ToolSampleGate` 统一持有固定题的预约和录音绑定。会话继续持有录音、线路、代次及原有同步锁。IPC 与识别保留原会话入口，录音开始、会话换代与停采在原位置通知准入模块。该模块只核对题目与录音归属，派发仍由原识别和任务链路处理。

`session_controller.py` 从 501 行降至 360 行。五项准入命令、三项生命周期转换和保留的 25 个会话方法通过结构对照。拆分前后各 105 项定向测试通过，12 组原生 AEC3 回放、校准及路由切换报告逐字节一致。覆盖正常对话、预约过期、换线、停采和不匹配转录，实机同样本回归仍待执行。

## 第二十步：识别回调与命令解析

将识别回调归入 `session_ports`，把唤醒、停止和休息指令的转录解析归入无状态的 `recognition_commands`。`RecognitionWorker` 继续管理有界队列、工作线程、识别准入和回执。固定工具题的本地对照在解码前和写回时仍核对原录音代次及预约身份，取消后丢弃迟到的对照结果。

`recognition_worker.py` 从 484 行降至 400 行。五个工作线程方法、四个回调类型、三个命令解析函数和原装配逻辑均通过结构对照。前后各 137 项定向检查通过，4 项私有录音用例按规则跳过。12 组原生 AEC3 回放、校准及路由切换报告逐字节一致。实体设备同样本回归仍需单独验收。

## 第二十一步：工具题识别对照结果

将固定工具题的双识别器分类移入原有的 `tool_asr_compare`，识别线程继续持有录音代次检查、预约归属及回执写入的锁。相同音频和分类规则沿用原路径，只有本地对照仍归属当前预约时才写入分类结果。原有请求派发、实际识别和音频处理保持不变。

`recognition_worker.py` 从 416 行降至 399 行。前后各运行 123 项定向检查，均为 119 项通过，4 项私有录音用例按规则跳过。12 组原生 AEC3 回放、校准及路由切换报告逐字节一致。实体设备同样本验收仍独立记录。

## 第二十二步：播报转录比对

将播报转录比对移到无状态的 `recognition_echo`，停止指令词表与指令解析一起由 `recognition_commands` 持有。识别线程直接调用这两个模块，`dsp` 保留旧导入入口供原调用方使用。播报文本和回声证据仍由原所有者提供。比较顺序、阈值、停止词优先级以及录音和会话的锁均沿用原实现。

拆分前后各运行 136 项定向检查，均为 132 项通过、4 项私有录音用例按规则跳过。12 组同样本原生 AEC3 回放、校准及路由切换报告逐字节一致。实体设备同样本验收仍单列。

实机回归在 0.15.29 与包含拆分的 0.15.30 上各执行一次。两次使用相同公开语音探针源码和原签名 APK 0.9.42／code79，八项检查均通过。两次都只接纳一条请求，确认设备播完和声学出声块，播后各观察 20.009 和 20.006 秒，独立复核双端停采。句尾至确认出声块分别为 4.161 和 5.673 秒。各只有一轮，无法将时延差归因于拆分。当前对照覆盖固定问句、播报、逻辑切换与停采，插话和实际 USB 总线往返仍按各自验收记录核对。

## 第二十三步：原生消回声适配器

将 SpeexDSP 和 WebRTC AEC3 的原生适配器移到 `echo_processors`，由 `EchoPipeline` 沿原有锁和工厂回调管理实例。共享的帧长、采样率与信号电平归入 `audio_signal`，`dsp` 保留这些既有导入入口，并继续持有播放参考和语音检测实现。Bridge 直接引用原生适配器，初始化、帧处理、参数及关闭顺序保持一致。

`dsp.py` 从 406 行降至 298 行，原有八个类与函数及两项音频常量通过语法树对照。拆分前后各 220 项定向检查通过、4 项可选私有音频用例跳过。同一批 12 组原生 AEC3 回放、校准和换线报告逐字节一致。现场版本尚未使用这一步拆分。
