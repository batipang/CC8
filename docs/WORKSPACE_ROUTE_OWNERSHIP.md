# 任务快照与连接激活绑定

此处0.9.10/code45保留当时源码验证；功能随后已随0.9.11/code46、后台0.14.16部署，见 [当前部署](CURRENT_DEPLOYMENT.md)。code45未单独安装，下方“未部署”指该候选阶段。

APK0.9.10/code45源码候选，后台仍0.14.16、协议2不变。实际运行仍为
0.14.13／APK0.9.7/code42，未部署本候选。

原设备接收路径只在分发中途检查Link对象；检查后解析／UI队列继续异步执行。
连接在这期间丢失、重连，或USB→Wi-Fi→同一USB对象重新激活，旧回调仍可能
把已经清空的任务放回界面、以旧大revision阻止新后台的小revision，或以迟到的
旧完成状态遮住“结果待核对”。旧link(true)排队到Home停采后执行也会误显已连接。

现在每次选择不同的活动通道、活动连接丢失及pause都使独立routeEpoch失效。
它不替代录音epoch、生命周期票据或回答generation，不放到网络JSON中。
分发入口在第一次解析前绑定当时的激活，普通事件继续通过当前激活检查；
workspace、操作结果提示和link的内部Java回调携带该激活，真正执行UI修改时再次核对。
同一Link重新激活也得到新值，不能仅凭对象相同接收旧回调。

UI进入新激活时清空旧任务/提醒及revision，并保留已知running工作的RAM-only
待核对提示。即使备用通道直接接管、没有中间link(false)回调，也执行此边界；
当前激活接受自己的低revision，之后继续拒绝较旧revision。既有真实终态、
无重交任务／无自动开麦，以及 [中断说明](INTERRUPTED_TASK_RESULTS.md) 保持。
关闭连接时在routeLock内取得通道和激活快照，再在锁外关闭socket。

十项自动回归编译真实select／lost／pause／closeSocket、分发入口与workspace
分支，以及MainActivity任务和连接回调；最小接口替代Android I/O，队列和解析
屏障控制交错，不使用sleep赌时序。覆盖两处解析阶段、断线、重连、同一Link
再次激活、已排队工作/连接回调、无false回调的备用接管，并保留当前终态／
revision、操作结果提示及非活动源的对照。b8062cb旧源码复现八处断言失败、零fixture error；
候选十项均通过。旧两步私有初探另保留，正式回归改用完整真实生命周期方法。

本测试不采集、不联网调用模型、不执行真实工具，也不替代Android实机调度、
物理拔插、任意旧网络记录的来源判定或完整P0验收。它绑定进入Java分发和UI
队列时的激活；没有宣称为网络协议新增每条快照的服务端激活证明。

完整本机installer23／host706（4项既有私有录音显式跳过）／tooling100／
Java15、固定格式/lint及八模块strict检查通过；Android正式源码编译与lint通过，
最小PATH下同样十项回归通过。远端CI仍须通过后才能合并。之前code44的六项
隔离设备UI结果保留为该版本证据，不改标为本轮code45实机验收；本轮未安装、
重启服务或开麦/拍照。

补充构建验证：从clean源码05e8a89生成无录音/拍照/悬浮窗权限、无shared UID
的独立QA包，Gradle构建/lint和原证书签名验证通过；摘要为
`2aedb6904170e79be24c43d0f9379c1ad02b42defbb60918b56e093230418d61`。
包未安装；设备只读检查为非助手前台、麦克风/摄像头关闭，保持该状态。

首轮PR76 CI（41639199412）保留：六个独立作业成功，但macOS在Set up job
下载固定setup-python Action时失败，尚未checkout/运行测试，Required checks
相应失败。随后从同一官方codeload地址成功取得HTTP200／1664686字节的归档，
官方commit API也确认固定SHA存在；不更换Action pin或放松必需检查。
本补充记录新构建证据及基础设施失败，后续文档提交仍须通过全部新提交CI。
