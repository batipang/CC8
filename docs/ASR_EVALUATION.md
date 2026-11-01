# P0-04：固定语料与私有评分工具

本轮先提供可以复用的评测软件，不根据合成数据调整 AEC、VAD 或识别门槛。当前选择仍是 MiniMax 耳朵 / 嘴巴、Dot 大脑；没有更新运行助手或采集音频。

## 语料和轮次

[asr-corpus-v1.json](../cc8-assistant/host/resources/asr-corpus-v1.json) 包含 75 条通用公开指令：普通问答、名字地点、时间数字、中英混说、停顿/轻声五类各 15 条；每类前 3 条是 calibration，后 12 条是 acceptance，共 **15 条调参、60 条验收**。参考正文和预先声明的关键字段在该文件中；没有真实用户转录或录音。

每条分别在 quiet/tv × 1m/3m 执行，共 300 个记录位。真实音量、位置和电视背景由操作人员按 TODO 矩阵安排，工具不会测环境声级，也不会控制播放/收音。停顿/轻声类别在同一版本内固定读法，并在 A/B 两轮保持一致；当前语料不是已经完成的真人采样。

命令只读用户提供的转录和人工标注。默认不保存音频；若未来需要录音回放，须先明确该轮授权、用途与保留时间，使用已有私有音频流程，不能因为填模板而自动采集。

## 准备空模板

从要保存结果的代码目录执行。下例不覆盖已有文件；输入和输出文件都放 owner-private 目录，评分文件不入 Git。

```sh
mkdir -p device-access/private/asr-inputs
chmod 700 device-access/private/asr-inputs
./cc8 score-asr --template device-access/private/asr-inputs/baseline.json
```

模板元数据 `engine=unspecified / version=0.0.0 / source=unspecified` 尚未赋值，按实际填写，例如 minimax / 0.12.2 / human。source 仅是操作人员声明，不会被评分器认证为真人证据。每行默认 `outcome=unmeasured`、空 transcript/fields；未测项不进入成功率或 CER/WER 分母，报告保留 missing。

真人测试时照语料朗读，在 CC8 字幕核对后填写识别原文。不要把参考答案复制进 transcript；没听到/没识别要分别填 not_detected / failed 和空 transcript，属于已测失败，按全部参考单位删除计分。recognized 即使空串也不算未测。

一条已测记录示例（示例只说明格式，不是实际成绩）：

```json
{
  "id": "names_places-04",
  "condition": "quiet",
  "distance": "1m",
  "outcome": "recognized",
  "transcript": "杭州的西湖有哪些特点",
  "fields": {"place": "杭州", "landmark": "西湖"},
  "intent_correct": true
}
```

fields 是操作人员核对的**实际听到/解释成的字段值**，只能使用该条语料预先声明的字段名。不能靠“转录中包含苏州”就判苏州正确，例如“不是苏州，是徐州”应标徐州。允许的等价值在语料中事先固定，不在看到错字后临时扩充。缺标注不计作正确；intent_correct 是人工核对任务意图的布尔标注，不是模型推断，不会执行指令。

## 评分与配对比较

```sh
./cc8 score-asr --input device-access/private/asr-inputs/baseline.json
./cc8 score-asr --input device-access/private/asr-inputs/candidate.json \
  --baseline-input device-access/private/asr-inputs/baseline.json
```

默认在忽略的 device-access/private/asr-scores/ 新建 0600 报告；也可用 --output 指定已有私有目录内的新 JSON。不会覆盖输入、已有结果、符号链接或外部替换的报告。输入要求当前用户普通文件、无 group/other 权限；重复 JSON 键、NaN、未知键、重复样本/场景、非法标注或超界值失败且不打印输入内容。

报告不保留参考文本、识别原文、实际字段值或路径，只保留语料 ID、分组、计数和差值。calibration / acceptance 在场景和类别统计中均分开，quiet/tv、1m/3m 不互相冒充。A/B 必须有相同已测 ID/场景、同一 source 声明和相同人工标注覆盖，否则拒绝比较；同一语料规范化哈希写入结果以绑定版本。

- Chinese Han CER：对 NFKC 规范化后的汉字计算 S/D/I 和字符错误率；不把阿拉伯数字或英语字母混入中文分母。数字错误必须同时看关键字段和完整转录精确数。
- English WER：提取英文单词，统一大小写/NFKC，保留词内 ASCII 连字符和撇号，计算 S/D/I；不推断同音词或把不同数字语义自动改成相同。
- 关键字段：预设等价值比较，保留正负号、小数点、单位，空白/全半角/大小写可规范化；需要的所有字段都标注后才进入整条关键字段正确率。报告同时给字段 expected/annotated/correct，缺标注不当成正确。
- 完整转录：规范化常规标点/空白，保留数值符号和数字间的小数点/冒号；单独报告精确相等条数。不能代替语义正确率。
- source、版本、距离和背景都是操作人员标签；人工意图正确率也显式给出标注样本数。零分母为 null；**没有“验收通过”自动结论**。指标可以超过 100% 错误率（插入很多内容），不截断美化。

输入最多 512 KiB、每条转录 1000 字符、75 条语料×4 场景且不可重复；动态规划累计最多 500 万单元，避免畸形输入耗时失控。CLI 使用标准库，不加载语音模型、不连接设备/网络。系统 Python 3.9 最小 PATH 已验证空模板→评分，300 项全为 missing，输出 0600；这只是工具验证。

## 后续真人验收

先用当前 0.12.2 建立基线，冻结验收语料及音量/位置，再对候选同样执行。调参只看 calibration，最终比较 acceptance；报告完整缺测和错例编号，按 TODO 的静音/电视与 1/3 米门槛判断，不能拿合成分数宣布准确率提高。插话起点到实际暂停、误打断、自播报和多人冲突仍需独立的真实声音测试，评分器不推断这些指标。

本机全套 installer 16、host 425（4 private-audio skip）、tooling 51、Java 15 checks，以及格式/lint、strict 六模块和脱敏扫描通过；新增评分回归 12 项，待真实 CI。
