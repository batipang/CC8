# 唤醒候选隔离对照：2026-11-03

2026-11-04补充：既有Whisper MLX缓存也未通过固定近音负例；三种配置均误触发Bumble by，名字提示另增加Bubble bee误触发。详见本文末尾的原始Whisper对照。运行后台0.14.17／APK48、原双语KWS、MiniMax ASR/TTS及独立72小时观察均保持。

本批没有找到可替换运行英文KWS的方案，P0-03继续开放。首批实验时现场为后台0.14.5／APK0.9.3；实验不修改运行模型、阈值或采集状态。

后续0.14.6～0.14.8的Dot回执、分段时钟和初始连接准备已随p0-candidate-20261103.2部署，仍保留原KWS、MiniMax ASR/TTS和Dot配置，见 [当前部署](CURRENT_DEPLOYMENT.md)。下面的Mac原生识别实验安装了独立系统英文资源，没有替换运行模型；“未改变依赖”仅描述前一批隔离模型实验。

## 输入与判定口径

标准V2为15条名字正例、10条普通或近音负例，沿用 [自动检查](AUTOMATED_DEVICE_CHECKS.md) 的六种production流条件。逐一核对25个样本ID、布尔标签及必填64位PCM摘要，全部相同；原版与BPE变体的语料及实际native二进制摘要也一致。没有删除失败样本、修改预期或将新原生库装入后台。

音素／英文CTC观察各33条：前述25条，加Shelley和Sandy两种命名音色各四条名字/近音输入；两组33条输入逐PCM一致。额外八条仅供已见开发观察，不声称新说话人独立验收。结果只在本机忽略目录保存公开合成数据和有界输出，无用户录音、麦克风、摄像头或设备操作。

## 两个关键词候选

| 方案 | 六种production条件的正例命中（各15条） | 负例误触发（各10条） |
| --- | --- | --- |
| 运行英文KWS基线 | 2、7、9、11、13、13 | 全部0 |
| 同一拼写的全部10种合法BPE路径 | 2、6、9、11、12、11 | 全部0 |
| 语音特征模板＋DTW | 每种15 | 5、5、6、6、6、7 |

列顺序为即时、60ms、140ms、160ms、480ms前导和1s连续上下文。这些是固定输入的条件观察，不是一般准确率统计。

BPE变体只增加同一`▁BUMBLEBEE`拼写的合法分词，不添加近音别名；仍使用原3.3M权重、max_active_paths4、score1、threshold0.25、trailing blanks1、gain2。表现反而下降，不能据此归因或批准修改分词。私有报告尾标识`32f28ae0`，开始模型评测时的固定缓存校验通过。

模板原型使用已有573条公开合成参考，12组训练音色不含标准V2的四个命名音色；提取1651个模板，其中240个正例。预先固定每句最多4窗口、16帧特征、每类候选4个、DTW带宽3、单帧更接近正例即命中；不在开发结果上调阈值。采用已有openWakeWord特征资产，逐资产摘要和573条参考PCM均核对。它补齐了本组正例，却误触发“Bubble bee”等负例；正例最大margin最低约0.00371，负例最高约0.06806，单一全局margin阈值不能同时保住本组全部正例并消除误检。报告`embedding-template-c3fe09d6/result.json`，全轮99.053秒；不部署、不据此声称正常房间误唤醒率。

## 两种编码器观察

为研究声学表示与拼写误差，另在独立venv中锁定25个依赖wheel及SHA-256，使用Torch2.15.0、Transformers4.57.6。没有安装进实际voice环境。模型从Meta官方仓库固定revision下载，逐文件核对大小、Git blob或LFS SHA-256；推理仅从本地读取、`weights_only=True`、固定库内模型类，不加载远程代码或访问云端。

| 观察模型 | 固定revision／权重SHA-256 | 观察范围 |
| --- | --- | --- |
| [多语音素模型](https://huggingface.co/facebook/wav2vec2-lv-60-espeak-cv-ft) | `ae45363bf3413b374fecd9dc8bc1df0e24c3b7f4`／`3173bde9e9ce490fa0f989e413c42f25bc1820c020adc1e6b9b87025b3cfcc5e` | 33条音素输出，尚无关键词判定器 |
| [英文CTC模型](https://huggingface.co/facebook/wav2vec2-base-960h) | `22aad52d435eb6dbaf354bdad9b0da84ce7d6156`／`c34f9827b034a1b9141dbf6f652f8a60eda61cdf5771c9e05bfa99033c92cd96` | 33条greedy文字输出及概率缓存，尚无关键词判定器 |

两份官方模型卡标明Apache-2.0。权重约1.26GB和378MB，均保留本机忽略目录，不随仓库发布。英文旧checkpoint缺少训练用mask向量：首次严格加载检查拒绝推理；核对固定Transformers源码后，仅在评估配置关闭训练增强，保持eval且不传mask，随后missing/unexpected/mismatched keys均为空。没有容忍随机推理权重，也没有修改原模型资产。

音素输出跨音色差异较大，不能当人工音标真值。英文CTC在标准15条名字输入中，仅2条去空格后精确为`BUMBLEBEE`，其余存在`BUMBLEBE`、`BUMBLEBY`等拼写；标准10个负例没有精确名字串。这只是整句greedy文字观察，不能换算为流式唤醒召回率，也不能通过接受所有近似拼写来宣布修复。

单句前向计算分别0.0814～0.1595秒、0.0309～0.0679秒；对应进程峰值RSS约1626.5MiB、777.7MiB。时间不含合成、模型校验/导入或设备/声学链路，不是部署冷启动或真实唤醒延迟。报告尾标识`47067a18`／`92a9d86c`；概率缓存逐文件冻结摘要，后续可重复比较关键词判定，而不反复生成同一批输入。

音色排查补充：Samantha、Daniel、Tingting的短名与本地化完整名，在同一句公开测试句上逐PCM相同；Tingting与显式系统默认也相同。这次没有发现短名/完整名选择差异；不据此断言合成发音完全正确或这些音色代表真人口音。

## 后续门槛

先用冻结的声学输出研究专用关键词判定与近音竞争，阈值和路径选择须使用训练/校准分区，再验证保留的全部失败样本与独立说话人。不得仅按已见开发分数放宽拼写、前缀或标签。需要额外检查连续流边界、普通话背景、自播报回声、资源/时延和模型许可；未完成前不接入等待唤醒路径。

同时尝试按官方Dataset Viewer查询公开LibriSpeech真人朗读片段；部分请求超时，少数查询返回0。尚未下载音频或形成完整真人基线，不能把这些响应当作语料不存在，也不能用合成/整句编码器观察关闭真人或实机验收。[数据集说明](https://huggingface.co/datasets/openslr/librispeech_asr)、[查询说明](https://huggingface.co/docs/dataset-viewer/search)。

## 冻结CTC输出的同段近音竞争

只读复用33份已校验的32维概率缓存。先用规范`BUMBLEBEE`路径选择13～120帧的片段，再在同一片段计算七种拼写的完整CTC前向概率：BUMBLEBEE、HUMBLEBEE、BUBBLEBEE、FUMBLEBEE、RUMBLEBEE、MUMBLEBEE、BUMBLEBY。不将另一位置的竞争词得分拿来比较，也不改音频、标签或模型。

规范路径相对逐帧自由最优路径的诊断分数，在标准25条上暂时分开：正例最低约−1.2063、负例最高约−2.2318；但额外近音输入已出现重叠，不能据此选择阈值。改看同段目标与最强竞争词的原始log概率差，标准正例仅6/15大于零，负例0/10；正例最低−7.2176、负例最高−0.9075，单一差值门槛也无法保留全部正例并排除全部负例。没有选择部署阈值。

报告尾标识`7d6c7204`／`28f58c43`，各33条。编码器和选段仍使用整句未来上下文，结果不是流式检测或唤醒延迟。拼写/音素输出不构成合成音发音的人工真值；原有近音负例和漏检样本全部保留。

## Mac原生文件识别对照

本机macOS27.2（26B123）、SDK中的Swift6.4.2，先只读检查：旧SFSpeechRecognizer授权状态为notDetermined，未调用请求授权；新SpeechTranscriber可用，已安装zh_CN／zh_TW，英文资源尚未安装。新API的处理范围及可用性以[官方SpeechTranscriber说明](https://developer.apple.com/documentation/speech/speechtranscriber)为准；[苹果权限说明](https://developer.apple.com/documentation/speech/asking-permission-to-use-speech-recognition)区分了旧服务与本地SpeechAnalyzer模块。这不代表旧识别接口已经获得授权。

固定使用`.transcription`与AVAudioFile，只处理前述标准25条公开合成输入，每条分别无提示／提示Bumblebee与大黄蜂。每次生成PCM后与既有摘要逐一核对；没有用户录音、麦克风、设备操作或生产配置变化。

| 文件识别引擎 | 无提示正例命中 / 负例误触发 | 名字提示正例命中 / 负例误触发 |
| --- | --- | --- |
| 已有zh_CN原生资源 | 0/15、0/10 | 0/15、0/10 |
| 新安装en_US原生资源 | 6/15、0/10 | 6/15、0/10 |

表中命中同时用原有生产名字正则复核，得到相同数量，没有放宽拼写或重新标注。英语模型仍将部分Karen输入识别为Gumblebee／Dumblebee，Tingting混说和旧默认名字也漏检。两批各50次文件调用，总轮次23.837／23.127秒，含合成、进程和报告I/O，不能换算为流式实时性能。名字提示没有补齐本组漏检，因此两种配置均不采用。

英文资源通过[AssetInventory安装接口](https://developer.apple.com/documentation/speech/assetinventory/assetinstallationrequest(supporting:))取得；该接口会自动为调用客户端保留语言项。实验检测到新增en_US保留项后，在相同安装客户端路径执行[release](https://developer.apple.com/documentation/speech/assetinventory/release(reservedlocale:))，确认该客户端从en_US回到原空列表，原zh_CN／zh_TW资源仍在。另一可执行文件的保护检查返回3、没有执行释放，随后使用原客户端正确完成；不把不同应用的保留列表混为全局状态。没有显式删除系统共享资源，新增英文资产由系统管理。

苹果资源没有这批外部权重同样的逐文件固定哈希，故记录为不透明系统资产；报告保留OS build、已安装语言、Swift源码/可执行文件摘要和安装/清理回执，不声称跨系统版本精确复现。私有报告尾标识`a22b6f9f`／`306e2655`，整体验证`research-verification-20261103-v2.json`。当前仍没有可部署的英文替代方案；后续优先补独立、发音可核对的真人参考，不能在已见合成输入上不断接受近音拼写。

## 公开真人参考：不计独立或远场验收

改用固定revision的Parquet文字列检索，完成test/validation四分区及train.clean.100，共18片、39665行；上次TLS失败的partial报告保留。本轮复用已完成分片，连接池和逐范围摘要缓存读取5329229个新数据字节，196.636秒完成；只解析id/text，预取范围排除音频列，未下载整片音频数据。依赖独立环境的六个wheel均固定版本和哈希，没有安装进运行环境。

语料为Vassil Panayotov等整理的[LibriSpeech（OpenSLR12，CC BY4.0）](https://www.openslr.org/12)。Parquet revision为`2b9f39377850ffce6bf6358257ae9f84b2349497`，音频资产源revision为`71cacbfb7e2354c4226d01e70d77d5fca3d04ba1`；对应源文件与转换分支的LFS引用相同。逐条核对row ID/行号/完整转写，再取四段短FLAC转为16kHz单声道PCM、冻结摘要；没有声称逐字节验证资产等于Parquet音频单元，也不保留签名临时URL到仓库。

| 公开片段ID | 来源 / 时长 | 原KWS六种production起点 |
| --- | --- | --- |
| 4362-15663-0050 | train.clean.100，15.21秒，含BUMBLEBEE | 均有名字命中 |
| 1320-122617-0034 | test.clean，9.485秒，HUMBLE | 均无命中 |
| 4446-2275-0001 | test.clean，4.66秒，FUMBLED | 均无命中 |
| 7902-96594-0024 | test.other，3.14秒，HUMBLY | 均无命中 |

使用当前源码、已校验的运行模型缓存和原native摘要26434ad0…；关键词、gain、threshold、路径数均不变。全轮2.206秒是数字处理时间，不是唤醒延迟。整句命中没有人工词位核验；六种条件是同一位正例说话人的重复观察，正例也处于所用英文CTC模型的训练分区，不能当独立ASR或一般唤醒准确率。现有合成语料的全部漏检/误触发记录不变。

这提供了新的真人组件参照，尚不足以选换模型或调整阈值。后续仍需更多独立正例、普通背景/自播报、流式边界与真实距离；本轮没有麦克风、云ASR或设备操作。报告`public-human-text-v2-a96b6324.json`、`human-english-kws-447ab1a6.json`及四段音频仅保存在本机忽略的私有目录。

## 既有SenseVoice资产的原始输出对照

另以仓库既有SenseVoice清单逐文件核对本机int8模型和tokens，在独立KWS评估环境调用实际OfflineRecognizer：自动语言、原ITN、CPU单线程，不经MandarinRecognizer的名字别名改写。标准V2的25条固定公开合成输入分别整句输入，前导0/160/1000ms、末尾160ms；每条保存PCM摘要与公开合成输出，音频临时生成后删除，未使用麦克风/云端或更换运行ASR。

三种条件均为0/15规范Bumblebee名字命中、0/10负例触发。整个模型/合成/处理/报告轮次25.098秒，不能当单次推理或流式唤醒延迟。部分输出是近音、字母串或中文音译，未通过增加这些别名来改变标签；该候选不采用。`raw-sensevoice-fixed25.json`与独立结论保留模型、native及脚本摘要；本轮仅排除这一原始整句ASR候选，不推断一般英文ASR表现，也不关闭P0-03。

## 既有Whisper MLX资产的原始输出对照（2026-11-04）

按[MLX官方本地模型接口](https://github.com/ml-explore/mlx-examples/blob/main/whisper/README.md)，仅在新的隔离环境读取已有缓存，不经MandarinRecognizer或名字别名改写。模型为[MLX Community固定revision](https://huggingface.co/mlx-community/whisper-large-v3-turbo/tree/a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb)，`a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb`，权重SHA256为`951ed3fc1203e6a62467abb2144a96ce7eafca8fa77e3704fdb8635ff3e7f8a6`，1613977612字节。四个文件的大小及Git blob/LFS摘要均与该revision API核对；对已有共享CAS链接核对解析目标、所有者及读取前后stat，没有下载或修改模型。加载后587个参数名和形状与safetensors头完整一致。

隔离环境为MLX/Metal0.29.4、mlx-whisper0.4.3、numpy2.3.4。30个依赖wheel固定版本/哈希并与PyPI发行元数据再次核对，离线按哈希安装、pip check通过，没有安装到voice/dev运行环境。准备阶段的CAS路径守卫及内置METADATA计数错误、依赖验证启动过早均留私有失败记录；修正后核对顶层发行名称/版本和完整内容，才开始推理。

先冻结三种配置与三种前导长度，再逐PCM核对原V2全部25条（15正例、10负例），共225次：自动语言、指定英文、指定英文＋固定提示Bumblebee；前导0/160/1000ms、尾静音160ms，temperature0、不带前次文本、sample_len64，保留原无语音阈值及严格名字正则。没有根据正负标签选配置、接受近似拼写或删除失败项。

| 配置 | 三种前导的名字命中（各15条） | 近音/普通负例误触发（各10条） |
| --- | --- | --- |
| 自动语言，无提示 | 13、12、10 | 1、1、1 |
| 指定英文，无提示 | 15、12、10 | 1、1、1 |
| 指定英文，固定名字提示 | 15、15、15 | 2、2、2 |

三种配置、全部前导均把既定负例Bumble by误当Bumblebee；名字提示还在全部前导误触发Bubble bee。提示补齐本组正例的同时扩大误检，**没有一种配置通过全部固定标签，本候选不接入运行唤醒路径**。不把整句ASR能力当专用关键词判别，也不反向修改负例标签；这些已见输入均为开发观察，后续不得冒充新留出。

全轮执行309.324秒，含导入、合成、模型加载及报告；模型加载0.7006秒，225次transcribe墙钟0.7761～2.0588秒、中位0.8218秒。使用整句未来上下文，不是连续流、真实唤醒延迟或声学链路性能。进程峰值RSS约1.08GB也不包括完整GPU/驱动内存核算，不能据此宣称常驻资源达标。

推理进程设置离线标记并拦截Python套接字连接/DNS/bind；报告计一次被阻止事件。独立的仅导入复现指向urllib3本地IPv6绑定探测，但原事件未存调用栈，不能追认其来源；没有允许推理网络请求。未加载用户服务配置、云服务密钥，未开麦、拍照、播放测试声音或改变生产实例。临时文件仅为公开合成输入，结束删除。

此转换卡未声明license且原模型链接为空；[原始上游卡](https://huggingface.co/openai/whisper-large-v3-turbo)标为MIT，不将其自动等同转换资产的完整授权证明。本轮不分发权重或接入部署。原声学服务、MiniMax分工和72小时只读观察保持不变。

私有报告`raw-whisper-fixed25.json`的SHA256为`6dd2e3c1279ba55634e56a5a34ae80646e49212cc11921e9880e7968fb4fcab7`，冻结计划SHA256为`f48832e7b21c4e988710d957b4883ead960dc3af7d0201382205307de55352e6`。终审核对25个ID/标签/原PCM摘要、225个结果及正则复算/汇总一致，研究进程已正常结束。另有七项无模型脚本保护回归及最小PATH/纯标准库计划验证，实际文件的固定Ruff检查通过；不关闭P0-03或一般真人验收。
