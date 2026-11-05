# P0 分轮诊断采集

`./cc8 measure` 在 Mac 上只发 `diagnostics` 请求；不启动模型、APK、麦克风、摄像头或任务，也不重启后台。先运行命令，看到“诊断采集已就绪”后再按评测矩阵进行操作。开始前已存在的请求（含尚在处理的请求）永久排除，避免把历史使用计入本轮。

```sh
./cc8 measure --seconds 180 --condition quiet --distance 1m --partition acceptance
./cc8 measure --seconds 180 --condition tv --distance 3m --partition calibration
./cc8 measure --help
```

软件和正在运行的安装目录不同时，可从新源码指定已有后台的本机 socket：

```sh
./cc8 measure --seconds 60 --socket /absolute/runtime/device-access/private/voice-runtime/announce.sock
```

这不会替换该后台或改变其麦克风状态。`condition / distance / partition` 是操作者填写的标签，不是自动测量出的噪声、距离或测试有效性；未填写即 unspecified。调参和验收应保存为不同轮次，不能拿本轮汇总自动宣布真人准确率达标。

## 范围和输出

- 单轮 0.05～3600 秒，轮询间隔 0.05～60 秒（默认 1 秒），最多 10000 个新请求。最后不足 10 毫秒时不启动一次新的查询。
- 同一 request ID 的 pending → 阶段补齐 → 终态只算一个请求；每次快照先完整校验，冲突或容量超限不部分提交、不挤掉旧样本。原后台内存仍只保留最近 128 条，采集进程在本机将观察到的轮次汇总。
- 只重建 ID、枚举类别、generation、单端阶段毫秒和结果等白名单字段；报告不带识别、回答、URL、提供方正文、异常正文、音视频或 socket 路径。不透传整个 IPC 对象；非法或含未知请求字段的快照使本轮停止并标为 invalid_snapshot。
- 默认输出到忽略的 `device-access/private/diagnostic-runs/`，目录须为当前用户私有，文件 0600。`--output` 只接受已存在的私有父目录中的新 JSON 文件；拒绝已有文件、符号链接父目录及外部替换，不 chmod 任意用户指定目录。
- 开始写一个 running 检查点，之后最多每 30 秒原子保存一次，结束或 Ctrl+C 再保存。强制杀进程可能丢失最后 30 秒内的内存更新，磁盘上的 running 不表示本轮完整。

stdout 只输出报告的绝对路径，提示写 stderr。退出 0 表示采集按时完成，**不表示样本质量或 P0 验收通过**；2 表示不可用、查询间断、非法快照、容量或输出失败等不完整状态；Ctrl+C 保存可得数据并退出 130。IPC 整次查询最多 5 秒，跨连接/发送/分片接收共用一个截止时间；最多连续 3 次只读失败后停止，不重试任何模型调用或设备动作。

如果相邻诊断快照的请求窗口已失去重叠，报告保留两边可用的记录，标为 `partial` 并退出 2。轮次截止后才返回的快照不纳入本轮，报告同样标为 `partial`，并用 `capture.late_snapshot_discarded` 明确记录原因。可用样本仍会汇总，缺失样本不补零。

## 固定一轮的后台来源

`--expect-backend` 要求每次诊断查询都报告指定版本和有效的后台实例。新版采集器也会默认核对相邻快照中的版本与实例。任一来源发生变化即停止，报告标为 `runtime_changed`、退出 2，并保留此前已验证的数据。候选版计时可在实际部署后这样运行：

```sh
./cc8 measure --expect-backend 0.14.40 --seconds 180 --condition quiet --distance 1m --partition acceptance
```

`capture.backend_version` 和 `capture.runtime_bound` 记录实际绑定情况。旧后台未提供完整身份时，省略 `--expect-backend` 仍可采集，但会明确标记未绑定。报告不保存实例 ID；版本字段来自后台自报，源码身份和设备 APK 仍需由部署记录核对。

## Dot 首轮分段（0.14.7 源码候选）

本次补齐分段计时，响应速度改善需依据实测推进。本节源码对照时现场仍为0.14.5；后续0.14.8已部署，见 [当前部署](CURRENT_DEPLOYMENT.md)。模型、推理强度和权限保持不变。新采集器兼容旧报告；旧采集器会按严格字段白名单拒绝新版字段，部署后台前应先使用新版采集入口。

| 阶段 | 本机观察点 |
| --- | --- |
| dot_rpc_start | 开始准备独立app-server；包含必要的旧上下文进程关闭 |
| dot_rpc_ready | app-server初始化及initialized发送返回；配套dot_rpc_reused布尔值 |
| dot_context_ready | 临时会话创建或复用完成；配套dot_context_reused布尔值 |
| dot_turn_start | 发出本轮turn/start前 |
| dot_turn_ack | 收到并确认有效turn ID的回应；时间采自回应返回处 |
| dot_first_delta | 首个明确final_answer的非空白文字增量通知；不包含commentary |
| dot_receipt | 最终文本通过一致性校验且收到成功turn/completed的通知 |

七种新耗时为`dot_rpc_prepare_ms`、`dot_context_ms`、`dot_preparation_ms`、`dot_turn_ack_ms`、`dot_first_delta_ms`、`dot_delta_to_clause_ms`、`dot_turn_complete_ms`。分别分解app-server准备、会话准备、整体大脑准备、启动回应、首段正文、正文积累到可播句，以及本轮权威结果。每项仍独立报告实际样本数/p50/p95，缺测不补零。

所有通知时刻在Mac本地单调时钟捕获，不使用提供方或Android原始时间。ID确认前有界缓存的通知保留原接收时刻；因此首段正文或完成通知可以早于dot_turn_ack。`task_result`仍记录实际任务入账时刻，与dot_receipt分别保留。旧协议仅有最终消息时，不伪造dot_first_delta；计时不等于模型内部首token生成或扬声器起声。

`dot_contexts.rpc`及`dot_contexts.thread`各按`new`、`reused`、`unreported`分组，再输出同一统计。缺少布尔值的旧后台或非Dot请求保留unreported，不能当新进程/新会话；也不把新、复用样本混为冷启动性能。布尔值与对应ready阶段原子入账，后续快照不能删除或改变已经观察到的模式。取消后的旧代次通知不进入新请求。

### 真实文字链路样本：2026-11-03

源码`f78bd94`通过实际独立app-server连续提交两次相同固定文字请求；首次新进程/新会话，第二次复用。每次收到两句正确正文和唯一权威完成回执，未观察到工具执行，结束关闭自有进程，owner配置摘要不变。无设备操作、麦克风、摄像头或TTS。原始有界报告保存在忽略的`timing-checks/actual-dot-20261103.json`，0600。

| 本次样本（毫秒） | 首次新建 | 本次复用 |
| --- | ---: | ---: |
| app-server准备 | 303.0 | 0.0 |
| 会话准备 | 2178.3 | 0.0 |
| turn/start到首段正文 | 4530.3 | 2444.9 |
| 首段正文到首个可播句 | 4.6 | 2.0 |
| 请求开始到首个可播句 | 7016.9 | 2447.2 |
| turn/start到完成通知 | 4674.6 | 2541.7 |

每组仅一条，不能当p95、稳定基准或证明复用导致全部差异。本次首轮约2.48秒用于准备，剩余等待主要在发出请求至首段正文之间；不能靠调整句子切分消除这段等待。后续可评估提前建立实际连接/临时会话，但不得用伪造模型预热请求、变更模型或推理强度冒充优化。现有运行服务未替换。

### 连接准备候选：0.14.8

新的显式前台收音epoch开始校准时，向既有串行回答队列提供一个可丢弃的连接准备项；重复epoch、后台初始化、普通重连都不产生该项。它只在当前仍有效且正在收音时，为默认Dot大脑建立初始app-server和空临时会话，不发送`turn/start`、不创建任务/历史/请求诊断行，不发送模型预热问题。当前模型、推理、技能和权限配置保持原样。已有会话或明确选中的聊天按原请求路径处理，MiniMax大脑不创建Dot连接。

准备与真实回答使用同一工作线程，避免并发创建上下文。队列满即跳过准备，不能挤掉真实工作；普通请求仍按原队列规则执行。手动停止后尚未消费的旧项无效；在途准备即使完成，也不能启用采集、发布回答或执行工具。准备中暂不允许切换聊天上下文；连接初始化沿用20秒预算，准备专用`thread/start`最多10秒，失败关闭该初始自有客户端，不自动重试，也不伪造一条回答失败。

实际第一条问题如果使用已准备的进程/会话，新计时会如实归`reused`；这不意味着模型已经回答过问题或缓存了答案。准备可能与原校准窗口重叠，但源码/组件测试不等于真实校准后的起声提速。本节隔离测试时现场仍为0.14.5，后续0.14.8部署另见当前部署记录。

源码`a2339b8`另从根目录、`PATH=/usr/bin:/bin`启动实际独立app-server验证：初始准备2111.0ms，期间实际RPC只有`initialize`与`thread/start`，没有`turn/start`、任务或工具事件。随后仅提交一次同一固定文字请求，复用准备好的进程/会话，答案正确且唯一完成回执，自有进程确认退出，owner配置摘要不变。请求到首可播句7793.8ms、到首段正文7787.1ms；单样本显示模型等待仍可能较长，不能用之前不同轮次的2447.2ms复用样本宣称普遍提速。这次没有跑设备校准或TTS，也没有声学起声验证；私有报告`timing-checks/actual-preconnect-20261103.json`。

## 收音与门控观察（0.14.10源码候选）

可选`capture`/`gate`补充每句已收尾后的数字声学摘要与识别门控分类，不保存PCM、识别或参考正文；字段、帧范围和固定枚举见 [门控观察](VOICE_GATE_OBSERVATIONS.md)。既有终态不因观察补齐而改变；采集器对旧值删除/改写、未知内容、非法来源或数值整体拒绝。缺测保持缺字段，新采集器兼容当前0.14.8。旧严格采集器不认识这些字段，部署新后台前须更新采集入口。

## 阅读统计

`summary / groups / scenarios` 给出样本数、终态、成功率和各耗时的样本数/p50/p95；缺少阶段或逆序时不填零、不加入该项耗时样本。成功率沿用后台规则，以 completed / failed / cancelled 为分母，rejected 和 pending 另列。`task_outcomes` 单独统计已执行任务，停止后续朗读不会撤销已完成工作。

### 已确认播放的语音回答

`voice_playout_coverage` 按场景单列已观察的语音请求。确认回答需要明确的语音准入、任务完成、请求完成，以及至少一个设备起播和完成相互匹配的段落。失败、取消、未决、被拒绝和缺少回执的记录各有计数。确认比例以本轮观察到的已完成、失败及取消为分母。后台 0.14.50 为首个确认段新增 `confirmed_playout_ack` 和 `confirmed_playback_done`。只有同一段起播和播完配对、且句尾不晚于该段起播的回答才进入 P50 / P95。较早的起播若缺匹配的播完证据，则用后续首个确认段的时间。缺少这对字段的历史报告保留确认段数，单列为计时缺测。原有阶段耗时仍可用于排障。

### 30 轮合成简单问答采样

```sh
./cc8 verify-device --runtime-root /absolute/runtime --capture --qa-latency-suite
./cc8 verify-device --runtime-root /absolute/runtime --capture --qa-latency-suite --execute
```

第一条只打印计划。第二条要求后台至少 0.14.50，并为同一后台实例和 USB 目标持有贯穿全程的测试锁。每轮单独限时开麦和校准，由 Mac 播一次带“大黄蜂”唤醒词的固定加法问句。30 轮各用不同的有序加数组合，逐轮核对识别标题和同段播放回执。计划和报告绑定同一套题目的 SHA-256，只列用例编号。播完后继续观察 20 秒，再确认停采并回到前台。遇到失败、误唤醒导致的额外回合、缺少阶段回执或未确认停采时立即结束，已通过的样本仍保留。

报告对已通过的简单问答样本给出句尾到 ASR、Dot 到首句、TTS 到首包和句尾到确认起播回执的样本数、P50 和 P95。统一使用这套固定语料对比版本间链路时延。实际扬声器出声、真人、答案语义、联网与工具任务各有独立验收。只在长稳观察结束后执行实机批次。

使用含 0.14.55 断句计时的主机和诊断工具时，批次另报 `device_vad_tail_ms`。它只统计收音时钟连续且有声帧可定位的样本。旧后台或不连续的帧不补零，也不影响已验证的其他阶段。该项不加到声学出声时延中。[断句时间的定义](VOICE_GATE_OBSERVATIONS.md#capture已收尾句子的数值摘要)

后台 0.14.57 及以上可为同一批次加入室内声学对照。

```sh
./cc8 verify-device --runtime-root /absolute/runtime --capture --qa-latency-suite --response-acoustic-evidence --execute
```

每轮按公开题库编号启用一次探针。报告单独给出设备时钟上“最后一个有声输入帧结束到首个确认的 20 毫秒输出块”的样本数、P50 和 P95。缺测或与该轮回答对不上时，报告保留此前通过的样本并停批，完成状态需要 30 轮全部通过。这个指标与起播回执分开，仍受声学环境和有声帧判定影响。报告保留 `audible_onset_measured: false`，真人实测仍单独验收。

### 联网与工具任务分别采样

后台 0.14.62 起，`--latency-scenario` 可选择固定的联网查询题或本机只读计算题。以下命令各打印 30 轮计划。长稳观察结束后，可分别加 `--execute` 执行，需要声学对照时可同时加 `--response-acoustic-evidence`。

```sh
./cc8 verify-device --runtime-root /absolute/runtime --capture --qa-latency-suite --latency-scenario web
./cc8 verify-device --runtime-root /absolute/runtime --capture --qa-latency-suite --latency-scenario tool
```

联网题只查询公开机构的官方网站，工具题只请求用本机终端做加法。每轮同时核对识别标题、后台记录的请求类型、同轮联网或成功退出的终端命令、以及完整播放回执。任何一项无法确认就停止该批次，保留此前合格的样本。简单问答、联网、工具分别绑定题库摘要并报告样本数、P50 和 P95。回答的事实与计算结果仍需单独核对。

### 离线复核既有报告

```sh
./cc8 audit-measure /absolute/private/run.json
```

复核命令从旧报告的请求记录重算，不读取原汇总或连接后台。输出保留采集状态、查询失败和窗口断裂计数，只输出汇总，不修改文件。起播回执的时延仍是代理指标，实体扬声器实际出声和整轮覆盖率需另外验证。

对历史 0.14.12 的三个合成算术问答批次复核了原报告摘要与 30 条唯一请求。排除一条播放失败后，29 条完成请求的阶段 P95 分别为 ASR 0.914 秒、Dot 到首个可播句 6.919 秒、TTS 到首包 0.389 秒，句尾到当时的首个播放头回执为 8.121 秒。旧报告没有同段起播与播完配对，按当前口径可用计时样本为零。这些数字可用于比较旧版各阶段的等待，不能计入当前版本的 30 次采样或声学出声目标。[历史批次记录](PAUSED_TAIL_DRAIN.md#实机定位)

所有阶段来自 Mac 单调时钟的请求内相对时长，不相减 Android 和 Mac 的原始时钟。`render_ack` 仅是播放数据回执；`render_ack_is_audible_onset=false`。新配对（后台 0.13.6 / APK 0.8.8）可增加 `playout_ack`：设备播放头越过首个非零 PCM 后的 Mac 收到回执时刻，`playout_ack_is_audible_onset=false`，其延时含轮询/传输开销，见 [设备播放回执](PLAYOUT_RECEIPT.md)。旧端缺少此阶段不填零。真实人声起点、扬声器出声与声学端到端耗时仍需后续实测，不能用此工具或 VAD 参数代替。

`coverage.window_discontinuities` 标出相邻非空窗口无交集或窗口变空的情况，可能由重启、窗口溢出或采集间断造成。即使为零，`complete_capture_asserted` 仍为 false：两次轮询之间可能错过已被窗口淘汰的请求。实际观察到的请求与人工固定样本编号还须核对，不能把无数据写成零延迟或 100% 成功。

该工具用于单轮诊断，不代替 P0-04 的真人 CER/WER/关键字段标注，也不代替 P0-05 的物理切换、真实待机和 72 小时资源观察。可观察的既有后台兼容性与测试记录见 VALIDATION；当前运行安装没有因此更新。
