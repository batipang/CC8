# P0 软件候选交接：2026-11-02

当前追加的代码检查候选为后台 **0.13.7**，APK 保持 0.8.8 code 33，修复及测试见 [代码检查](CODE_REVIEW_20261102.md)。该后台修复没有重建下方旧快照或替换现场运行服务。

本页下方固定记录 p0-software-20261102.1 快照。后续工作候选已到后台 **0.13.6 / APK 0.8.8 code 33**，新增[设备播放头回执](PLAYOUT_RECEIPT.md)，见 [PR #39](https://github.com/batipang/CC8/pull/39)。原版本文件/回执已保留在本机忽略的 artifacts/preserved/p0-software-20261102.1；不要将下方旧 APK 哈希用于新版。两批均未替换现场安装。

本轮可独立完成的软件改造已合入 main，**还没有替换正在运行的助手**。源码标记 [p0-software-20261102.1](https://github.com/batipang/CC8/tree/p0-software-20261102.1) 固定到 `5f1c087cfa8c4cdea42758dd26b7f0597b1c31ef`，该提交已由 PR #37 经全部 CI 通过并包含在主分支；后续 main 的交接文档提交不改写这个源码快照。

| 组成 | 候选 | 现场已知运行 |
| --- | --- | --- |
| Mac 后台 | 0.13.5 | 0.12.2，未重启/升级 |
| 助手 APK | 0.8.7 / code 32 | 0.8.6 / code 31，未安装替换 |
| 检测器 APK | 1.0.1 / code 2 | 没有因本轮软件工作重装 |
| 通信协议 | 2；设备接受 2～2 | 2 |

## 软件交付与证据

- 请求追踪和 `cc8 measure`：有界、去重、内容白名单和私有检查点；区分任务执行、语音合成、设备回执，不把 render_ack 当真实扬声器起声。见 [分轮诊断](MEASUREMENT_RUNS.md)。
- 后台恢复：进程所有权锁、十次真实隔离模拟子进程退出/恢复、disabled 停止及不健康替换；不恢复已交办写操作或开启采集。见 [后台基础](P0_FOUNDATION.md)。
- 双本地唤醒：中文共享停止模型独立 stream、英文固定专用 BPE 模型，云 ASR 前双门控、有界空闲窗口、同句缓冲；原停止参数/无唤醒词/后台与重连规则保留。见 [LOCAL_WAKE](LOCAL_WAKE.md)。
- 固定识别评测：15 条调参 + 60 条验收语料、300 个四场景记录位，空模板全未测；CER/WER、人工关键字段和意图、相同覆盖 A/B、私有无正文结果。见 [ASR_EVALUATION](ASR_EVALUATION.md)。
- 长时观察：`cc8 observe` 只读自身资源和健康曲线，按运行实例分段，旧后台缺字段/中断明确 partial，不自动判无泄漏或验收通过。见 [RUNTIME_OBSERVATION](RUNTIME_OBSERVATION.md)。
- 故障与收尾：按 ASR/大脑/TTS/播放/连接分类；拒绝审批无虚构待批准状态；完成任务不因 TTS/断连降级；MiniMax 检索失败不算完成；取消通知失败仍释放本地采集/唤醒 waiter，失败开场不残留校准。见 [FAILURE_FEEDBACK](FAILURE_FEEDBACK.md)。
- 工程前置：统一轻量测试、格式/strict 边界、强制 CI/安全扫描、schema/版本/原签名、单实例 setup/doctor/run、十步职责拆分和兼容性/贡献/私密安全报告文档。实际工程截图仍未补，许可/对外角色方案没有擅自变更。

最终本机离线：installer **22**、host **444（4 项私人音频依赖跳过）**、tooling **51**、Java **15 checks**，格式/lint/strict 六模块通过。新动态/native 组件不冒充 strict 全覆盖。最后代码 [PR #37](https://github.com/batipang/CC8/pull/37) 的 [真实 CI](https://github.com/batipang/CC8/actions/runs/41357130621) 八项全部通过；先前各批证据见 [VALIDATION](VALIDATION.md)。没有读取私人录音、调用云模型或打开设备采集来完成本轮验证。

真实双 KWS 额外处理 30 分钟合成低电平噪声（约 40.6 秒加速），无触发、后段 RSS 采样稳定；另有五条名字合成正样本/三条普通句负样本。以上不是真人准确率、实际时长或真实房间 AEC 结论。

## 本机产物

文件位于本仓库忽略的 artifacts/；没有上传 APK、创建 GitHub Release 或打包私钥/配对/配置/权重/录音。源码压缩包只来自 git archive 的已标记文件。

| 文件 | SHA-256 |
| --- | --- |
| cc8-assistant-0.8.7.apk | `05520cd42f480c4ac9fc9c5aac0c13008128c313f014a3e683a81d3dba440262` |
| cc8-diagnostics-1.0.1.apk | `830290358b0f64b1b12c9545f2b9792cbc66336ee88839b1146ac662809f84df` |
| CC8-p0-software-20261102.1-source.tar.gz | `57bb5533cb415850e2ade89cfd52e0a10e92d912b70d51912f31b6e9e141485c` |

两个 APK 使用原 JKS 路径引用，实际 assembleRelease/lintRelease、SDK27 签名和版本核对通过，证书集合与此前已验证的原签名清单一致。APK 内容与此前同版本产物相同，本轮主要变化在 Mac 后台和工具；构建回执重新绑定到本次干净源码 SHA。

`artifacts/releases/p0-software-20261102.1.json` 的 record / verify 已实际通过，`source_dirty=false`、tag 与上述 SHA 一致、device_acceptance 固定 not_asserted。源码标记已推送并核对远端 annotated tag 解引用值；没有用源码/产物核验冒充新一次设备证书读取或安装。后续 main 移动后复验要回到该 tag 且保留对应产物，详见 [RELEASES](RELEASES.md)。

## 下一轮的输入

按用户“稍后配合，先完成软件”的安排，保留当前运行安装和采集状态。后续部署需在原目录明确停止旧服务、保留原签名/配对/私有配置，并准备新增英文 KWS；全局运行所有权检查不会替用户停掉另一工作区。

真人固定语料/唤醒/普通音量插话与自播报负样本、真实 USB/Wi-Fi 物理往返、三轮 15 分钟黑屏唤醒、登录/睡眠联调和 72 小时观察仍须按 [唯一 TODO 的验收矩阵](TODO.md#固定评测矩阵与首轮门槛) 取得证据，再根据基线决定是否调参。没有把这些 P0 整项勾选完成，也没有自动开始长时观察或安排用户现场操作。
