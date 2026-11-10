# 真实登录后的后台恢复收据

P0-02 增加一次布置、下次真实图形登录后自动比对的入口。后台候选0.16.26，Android仍为0.9.97／code134；实际运行仍是0.16.24。本批不注销、不休眠、不重启服务，不开启CC8界面、麦克风、相机或模型请求，也没有实际保存生产基线。

默认只输出计划，系统Python3.9和最小PATH可用：

```sh
./cc8 verify-login --runtime-root /Volumes/AI/Codex/CC8-P0 --arm
```

在包含新观察器的后台正常部署、版本稳定且管线空闲后，明确保存一次基线：

```sh
.toolchains/cc8/dev-venv/bin/python cc8 verify-login \
  --runtime-root /Volumes/AI/Codex/CC8-P0 --arm --execute
```

执行需要Python3.11+和已有启用的原LaunchAgent；不能由新版CLI替旧运行目录制造一个没有观察器的基线。它核对当前图形登录域、原配置／完整ENV、私有资料摘要、源码版本、注册的监督进程及后台归属和空闲状态，再保存元数据。不会安装登录项或改写原服务配置。基线7天有效；这段期间更换源码、配置、私有资料或再次执行原服务install/start，都会使原比较条件失效，不能据此标通过。

之后只需按正常需要使用Mac。下次原监督进程启动时，观察器默认关闭；仅有明确保存且仍处于armed状态的基线才启动一次有限观察。仍是同一GUI登录域时保持armed，不轮询健康状态，不把崩溃恢复、手动重启、Unix SID或PID变化当作新登录。不会为了验证而注销或重启电脑。

新GUI域由当前macOS的`launchctl print gui/<uid>`诊断输出确认，结合本次启动UUID和随机诊断ID生成摘要，不保存原UID、ASID或启动UUID。格式未知时保留未确认。当地安装的`launchctl(1)`说明login域在GUI登录时创建，并由该次audit session标识；`gui/<uid>`是同一登录域的另一种定位方式。这里使用的是显式诊断入口，不依赖`audit_session_self`私有API或用后台进程会话号代替它。

新域中就绪预算180秒，正在进行的单次OS／IPC／源码读取各有自身有界上限，超预算返回不能算通过。核对原源码／配置和注册的唯一服务，后台就绪后再确认同一实例稳定至少3秒，采样缺口超过2秒重新累计，落盘前再核对当前就绪和空闲。全新后台须没有采集／播报／ASR／请求活动；若原服务接管仍存活的后台，则必须有当前监督的6秒内ready回执，并与基线逐项比较原活动计数和请求摘要。缓冲的健康回复之后进程已退出、所有者开始工作、资料改变、取消、超时或时钟异常，均不能标通过，也不控制用户新任务。

结果范围是**新图形登录域中的后台就绪与原配置保持**，明确保存`automatic_launch_cause_verified=false`：它没有独立证明launchd当时的全部触发原因。连接状态如实记录，即使后台就绪时没有CC8连接也不伪称USB／Wi-Fi或语音恢复通过；设备、真实系统睡眠和完整P0仍按原标准单列。时间从观察开始算起，不是登录界面出现到语音可用的精确时延。

查看或取消只访问该诊断文件：

```sh
.toolchains/cc8/dev-venv/bin/python cc8 verify-login --runtime-root /Volumes/AI/Codex/CC8-P0 --status --execute
.toolchains/cc8/dev-venv/bin/python cc8 verify-login --runtime-root /Volumes/AI/Codex/CC8-P0 --cancel --execute
```

单次元数据在实际runtime的`device-access/private/voice-runtime/login-check.json`，目录0700、文件0600；输出不包含比较基线、ENV、路径正文或配置内容。旧观察者不能覆盖取消或新诊断。发生异常后不自动重试；观察中进程被强制结束可能只留下observing，不能当成完成。明确重设前须先取消尚未终结的检查；终态会原样保存到私有history，最多16份，满后拒绝新增，不淘汰历史失败。

本批软件回归覆盖模拟GUI域变化与普通重启、接管、实际本地Unix socket、权限／配置／源码变化、过期／取消／迟到回写、私有归档及可选检查失败不能影响服务清理。当前Mac只读解析成功，不是一次真实重新登录。首轮后台钩子夹具将新旧时钟混用而错误过期，修正夹具后通过；硬链接锁回归先复现权限可被修改，再修复为修改权限前拒绝多链接文件。原失败保留，不把它们改记为首次通过。实际新登录环境结果与最终72小时仍待后续，72小时优先级最低。
