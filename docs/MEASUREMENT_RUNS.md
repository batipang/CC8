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

## 阅读统计

`summary / groups / scenarios` 给出样本数、终态、成功率和各耗时的样本数/p50/p95；缺少阶段或逆序时不填零、不加入该项耗时样本。成功率沿用后台规则，以 completed / failed / cancelled 为分母，rejected 和 pending 另列。`task_outcomes` 单独统计已执行任务，停止后续朗读不会撤销已完成工作。

所有阶段来自 Mac 单调时钟的请求内相对时长，不相减 Android 和 Mac 的原始时钟。`render_ack` 仅是播放数据回执；`render_ack_is_audible_onset=false`。真实人声起点、扬声器出声与声学端到端耗时仍需后续实测，不能用此工具或 VAD 参数代替。

`coverage.window_discontinuities` 标出相邻非空窗口无交集或窗口变空的情况，可能由重启、窗口溢出或采集间断造成。即使为零，`complete_capture_asserted` 仍为 false：两次轮询之间可能错过已被窗口淘汰的请求。实际观察到的请求与人工固定样本编号还须核对，不能把无数据写成零延迟或 100% 成功。

该工具用于单轮诊断，不代替 P0-04 的真人 CER/WER/关键字段标注，也不代替 P0-05 的物理切换、真实待机和 72 小时资源观察。可观察的既有后台兼容性与测试记录见 VALIDATION；当前运行安装没有因此更新。
