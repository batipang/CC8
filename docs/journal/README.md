# 历史与证据索引

本次只建索引，不把旧记录改成当前版本的能力，也不迁移已有入口。

| 阶段 | 原始入口与证据边界 |
| --- | --- |
| 2026-11-01：单机安装/相机/语音与 UI | [VALIDATION](../VALIDATION.md) 对每节单独记录版本；[CODE_REVIEW](../CODE_REVIEW.md) 保存原检查上下文 |
| 2026-11-01：功能规划 | [竞品报告](../COMPETITOR_REVIEW_2026-11-01.md) 是当日来源核对与设计建议，不是当前功能全已完成 |
| 2026-11-02：开发/CI/边界/发布基础 | [CI_SECURITY](../CI_SECURITY.md)、[协议](../PROTOCOL_BOUNDARIES.md)、[版本](../RELEASES.md)、[VALIDATION](../VALIDATION.md) 的具体 PR/CI 链接 |
| 2026-11-02：十步职责拆分 | [BRIDGE_REFACTOR](../BRIDGE_REFACTOR.md)，纯重构和独立缺陷修复分别留痕，实机/声音回归缺口未关闭 |
| 2026-11-02：P0 软件候选 | [本地唤醒](../LOCAL_WAKE.md)、[故障分类](../FAILURE_FEEDBACK.md)、[评分](../ASR_EVALUATION.md)、[资源观察](../RUNTIME_OBSERVATION.md)；不把合成检查当真人成绩 |

今后新增实验建议使用 `YYYY-MM-DD-topic.md`，记录目标/基线提交、方法/授权范围、实际结果、跳过与失败、是否部署，以及与 TODO 的关联。原始音视频、凭证、设备唯一标识和完整私有报告不放这里。任务状态只更新 TODO，历史记录不回填虚构通过。
