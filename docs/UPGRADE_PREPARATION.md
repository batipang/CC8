# P1-09：离线升级准备与非密钥配置恢复预生成

后台工具候选 0.14.1，APK 仍为 0.8.9 code 34，协议 2。P0 待测源码固定在 p0-validation-base-20261102.1。以下命令不安装、不启停服务、不联系设备/模型，也不会替换配置；当前运行安装未改变。

## 版本规划

```sh
./cc8 upgrade plan --from-root /absolute/baseline-source --to-root /absolute/candidate-source
# 可选：使用已有 Android 工具实际检查候选包版本与 API-27 签名
./cc8 upgrade plan --from-root /absolute/baseline-source --assistant-apk /absolute/candidate.apk
```

两个目录必须各有经现有 release schema 校验的 release.json。报告独立 APK/后台/协议版本、已校验版本声明摘要，分别检查“旧设备协议配新后台”“新设备协议配旧后台”，标明 APK versionCode 降级。这里比较的是**源码声明，不是已装版本探测**。选填 APK 检查复用 tools/release.py，不调用 adb；校验候选签名不等于已核对设备原签名。

结果固定 ready_for_installation=false、installed_signer_checked=false、device_acceptance=not_asserted。协议任一方向不兼容时写报告并退出 2；退出 0 只表示声明检查完成。原签名/实际已装版本、服务所有权、配对/资源和物理切换仍须在正式部署流程核对，不能由一份 JSON 自动批准升级。Android 降级仍有平台限制，工具不会卸载、清数据或强制降级。

0.16.24增加定时汇报索引格式门禁，与既有提醒格式独立检查。`report_watch_storage`只输出文件是否存在、格式版本和目标reader声明，不导出来源、正文或索引；`plan`和三包`verify-transition`都检查现有私有文件。旧数量配置仍为schema1；只有明确启用摘要才写schema2。基于0.16.23的恢复包不能读取schema2，门禁会拒绝该恢复，不能覆盖或还原旧索引来绕过。若用户明确清除定时配置／索引，或明确重新设置为数量模式并接受从现在重建索引，保存格式可回到schema1；规划工具本身不做这些修改。恢复准备通过只证明当时文件兼容，真正恢复前须按当时文件重新检查。

### USB 认证切换

从采用配对 TLS 的候选开始，版本清单单独声明 USB 认证方式。旧清单未声明时，报告标为未核实；已知认证方式不同时，混合版本的 USB 不兼容。应用协议一致仍允许离线完成签名和产物检查。报告会同时列出升级和回退方向，并注明需协调更换双端；它始终要求对已配对 Wi-Fi 单独核对连接状态。

安排维护时先确认当前实际运行版本、原签名及已验证的恢复包，保留原配对。以已核实的 Wi-Fi 作为混合版本期间的可选通道，或预留短暂离线窗口协调更换两端。用不自动开麦的维护入口确认新组合重新连接，再做实体 USB 优先、拔线后 Wi-Fi、插回后 USB 的回归。回退时同样按报告协调两端和独立核对连接，保留原配对，不恢复明文 USB 到新后台。

## 校验升级和恢复包

`verify-transition` 比较三个干净的 Git 工作区。基线 APK 的 SHA-256 必须与已核实的安装记录一致。工具校验三个 APK 的包名、版本和 Android 27 签名，要求签名证书一致、升级和恢复的 versionCode 逐步增加。它还核对两份新包的构建回执和双向协议兼容；恢复源码必须保留基线运行行为，只改 `release.json`。为保留固定 Gradle 下载源修复，允许额外携带 `.github/workflows/ci.yml` 和 `tools/tests/test_gradle_provision.py`，但每份必须是与候选提交完全相同的普通、非可执行 Git 文件。缺失、删除、链接、可执行位或内容不一致均拒绝，其他路径仍拒绝。报告记录实际差异和匹配的构建支持路径，不把这项检查当成设备安装授权。

```sh
./cc8 upgrade verify-transition --from-root /absolute/running-source \
  --to-root /absolute/candidate-source --recovery-root /absolute/recovery-source \
  --baseline-apk /absolute/verified-baseline.apk --baseline-sha256 <installed-apk-sha256>
```

候选与恢复包从各自工作区的 `artifacts` 读取。输出为私有报告。命令只检查本地文件，无需签名钥匙、ADB 或运行中的设备。安装前仍须独立核实当时设备上实际包的版本、摘要和证书。

可追加 `--settings-backup /absolute/private/backup.json`，在同一流程核对备份能否用于候选和恢复版本。报告记录两个目标的兼容结论、可省略的默认字段名，以及原备份中规范化设置的 `settings_sha256`。任一目标不兼容时停止，不生成成功报告或恢复配置。该选项不读取运行时环境覆盖，也不应用设置。

实际核对或安装时可给 `cc8_install.py --verify-apk` 或 `--install` 加 `--expect-installed-sha256 <verified-baseline-sha256>`。如果当前已装包缺失或摘要已变，命令会在传送安装助手前停止。同签名但不同构建也会被拒绝。回退前先确认实际已装的是哪一个包，并使用与其对应的已核对摘要。再用 `--expect-apk-sha256` 传入本次预检报告的 `stages.candidate.apk.sha256` 或 `stages.recovery.apk.sha256`。这一摘要直接核对安装器本次所读的私有字节快照，验证后的相同字节才交给签名检查和安装。仅运行 `--verify-apk` 时也可传入两份摘要，输出含本次读到的 APK 摘要。

两边携带同一份 CI 文件时，可选 Mac 窗口组件的检查仅在其 `mac-window-input/Package.swift` 存在的 macOS 工作区运行：候选包含该组件，须运行合成检查、XCTest 和构建；保留旧功能的恢复源码没有该组件，可跳过这一组件步骤。主后台测试和八项工作流检查不随之跳过，不为让恢复 CI 通过而把候选运行时代码复制到恢复源码。

若需要把 72 小时观察作为升级前置，在同一命令追加 `--observation-report /absolute/private/window.json`。工具按基线源码的后台版本复核私有报告，重算采样和时间完整性，并在升级报告中记录观察摘要及其规范化 SHA-256。窗口仍在运行、版本不符或采样有缺口时，输出失败原因并退出 2。旧版观察只绑定后台版本及单实例，实际设备 APK 的身份仍在安装前独立核对。

本次使用原签名完成后台 0.14.33／APK 0.9.18 code53 候选与后台 0.14.20／APK 0.9.19 code54 恢复包的编译、静态检查和离线校验。恢复包保留现用功能源码，版本号高于 code53，满足恢复覆盖的版本顺序。现场仍运行 0.14.20／code49，升级和恢复均未执行。

## 显式备份与校验

```sh
./cc8 upgrade backup --profile /absolute/original/.toolchains/cc8/settings/assistant.json --source-root /absolute/original
./cc8 upgrade verify /absolute/private/backup.json
```

只读这份显式选择的 owner-private 非密钥配置文件，复用原 schema、大小、重复/未知字段与权限校验。原 asr-only legacy 格式可规范化到 schema 1；其他缺版本输入仍拒绝，不放宽原配置规则。

快照按 schema 默认值、原工作区的仓库配置、指定私有文件三层依次覆盖。原仓库配置缺失时使用 schema 默认值；存在但无效时停止备份。**不采集环境覆盖或服务定义**，因此部署前仍需按 CONFIGURATION 核对原服务中的显式配置。source-root 指原运行工作区，用于固定相对凭据/原签名引用；不读取所引用文件。原默认签名路径也固定为原工作区绝对引用，避免换目录后生成或使用另一套钥匙。备份与恢复不能证明这些引用文件存在或符合原证书。

不复制凭据、私钥、配对材料、模型、录音、提醒或任务结果。输出含私有路径引用，因此只能保留本机私有目录，不提交 Git。默认写忽略的 device-access/private/upgrade-preparation，目录 0700 / 文件 0600；可用 --output 指定已存在的私有父目录中的新 JSON。拒绝已有文件、链接或公开父目录，不自动 chmod 外部目录。

## 恢复文件预生成

```sh
./cc8 upgrade stage-restore /absolute/private/backup.json --output /absolute/private/staged-settings.json
```

为旧版恢复准备配置时，加 `--target-root /absolute/path/to/recovery-checkout`。工具会读取目标版本声明的配置 schema，检查现有字段是否兼容。旧版未声明 GPT-Live 字段时，仅在字段仍为默认 `minimax` 和 `marin` 时省略相应字段。显式启用试用、指定其他试用音色、目标缺少 schema 或其他字段契约改变时，命令会在写文件之前停止。原备份保留全部设置。此检查核对目标声明，服务环境和启动仍须单独验证。

恢复到只支持 Dot 和 MiniMax 的版本时，保留原来选中的 Dot 或 MiniMax。配置选择了旧版不支持的本机复述 `echo` 时停止暂存，避免改变所选大脑。

若已用上述升级预检核对备份，可给 `stage-restore` 同时传入 `--expect-settings-sha256` 和预检报告里的摘要。即使换成了另一份自身校验有效的备份，只要设置内容不同，也会在创建输出前停止。摘要对应省略默认字段前的设置。

在实际选用配置副本之前，可再次比对备份、预检回执、目标提交和副本内容。升级填 `candidate`，恢复填 `recovery`，并分别提供对应的 Git 工作区。此命令只读，拒绝目标提交或副本内容发生变化，输出不含设置值。

```sh
./cc8 upgrade verify-staged --transition /absolute/private/transition.json \
  --settings-backup /absolute/private/backup.json \
  --staged-profile /absolute/private/staged-settings.json \
  --target-root /absolute/candidate-source --stage candidate
```

先校验 backup schema、规范化 settings 和 SHA-256，再创建**新** settings 文件。SHA-256 用于内容完整性检查，不是发布者签名。备份中的数据不决定恢复路径，输出只能来自命令明确指定；不允许覆盖原配置或已有文件。损坏、篡改、权限错误、未知 schema 和写失败不会改动原配置。

本命令不应用 staged 文件。真正升级/恢复仍须先明确停止原服务，核对原签名/配对/资源和配置环境，将核对后的配置应用到目标，再按 SIGNING / RELEASES / TODO 做实机验收。没有自动回放工具任务、播报或开启采集。

## 软件验证范围

十项离线回归覆盖双向协议、降级标记、复用 APK 检查、原配置不变、引用固定且不读钥匙/环境密钥、legacy 规范化、权限/链接/不覆盖、篡改/重复/超长输入、只读 verify 及最小 PATH CLI。所有备份使用合成配置。

另在系统 Python 3.9.6、PATH=/usr/bin:/bin 实际跑 root CLI：从固定 P0 source tag 的 manifest 到候选规划，对本机原签名 APK 0.8.9 实际 AAPT/API-27 检查；再用合成配置 backup → verify → stage-restore。全部通过，文件 0600，原合成文件字节不变，未读取真实运行配置或操作设备。完整批次见 [VALIDATION](VALIDATION.md)。

P1-09 仍开放：真实升级/回退演练、服务配置应用与设备验收尚未执行。

## 私有数据恢复

在实际运行后台的源码目录使用该后台配置的语音环境 Python 检查指定存储。例如，检查损坏后可能影响启动的提醒文件：

```sh
.toolchains/cc8/voice-venv/bin/python ./cc8 recover-storage reminders
```

检查复用后台相应的加载规则，只输出状态与 SHA-256。确认返回 `unreadable` 后，先停止同一目录的后台，将检查返回的摘要赋给 `SHA256` 后执行隔离。

```sh
.toolchains/cc8/voice-venv/bin/python ./cc8 recover-storage reminders --quarantine-sha256 "$SHA256"
```

命令核对服务和运行锁、文件所有权及本次摘要，把原文件移入同级的私有恢复目录。隔离成功后，重新启动的后台以该存储的空状态继续运行，原件仍可从回执给出的路径核对。若状态为 `quarantined_durability_unconfirmed`，先核对回执中的原件路径。检查和隔离均不启动服务或设备采集。

支持的存储名为 `reminders`、`preference-memory`、`recognition-vocabulary`、`workflows`、`saved-task-results`、`work-report-sources`、`work-report-archive`、`work-report-progress` 和 `work-report-watch`。原 `recover-reminders` 命令仍可用于提醒。身份、配对和凭证应按各自授权流程处理。
