# 2026-11-02 代码检查与修复

检查基线为 main `84ff49fedd1e4b58fc8964815ddca47bf35b91cd`。本轮重点核对后台播放/会话/识别/回答、传输与资源收尾，运行全套离线和固定质量检查。没有访问私人录音、打开麦克风/相机或改动当前运行服务。以下均是可复现问题，不以静态推测作结论。

| 优先级 | 问题及触发 | 修复 |
| --- | --- | --- |
| P1 | play_done 先唤醒 renderer、后清 playing；renderer 已开始同一回合下一句时，旧 reader 又把 playing 清成 false，破坏插话与空闲判断 | 在短时 publication 锁内先发布完成状态，再通知 completion；后续句发布使用同一锁，锁内不做网络 I/O |
| P1 | 播放完成超时、设备报错、流式语音异常的 generation 检查与取消分离；新请求可插入间隙，旧错误随后取消新 generation、覆盖 UI | 检查、取消及固定错误反馈属于同一 session owner 事务；等待完成超时在事务内再次核对 ACK，维持 session → publication / completion 的既有锁序 |
| P2 | 插话暂停超时虽已保护取消，但 listening 提示在事务外发送，可覆盖新请求的 thinking 状态 | 把对应状态反馈留在同一事务中 |

## 回归证据

新增 [test_playback_completion_races.py](../cc8-assistant/host/test_playback_completion_races.py) 四项测试，用真实线程、明确 barrier、有限等待和 finally 清理覆盖：连续两句完成/发布交错；完成超时与新 owner 请求；设备错误和 iterator 错误与新 owner 请求；插话超时状态交错。测试没有靠长段 sleep 或真实设备来碰运气。

同一测试套件实际替换为基线控制器的对应方法后，得到 **五处预期断言失败、零 fixture error**；修复后通过。保留原播放/暂停死锁、提前 playout ACK、取消/断连和错误分类测试，没有削弱断言或跳过检查。

此轮只修改后台行为；助手 APK、音色、声学阈值、协议、连接优先级和权限不变。软件版本区分为后台 0.13.7 / p0-review-20261102.1，未部署。完整回归和 CI 以 [VALIDATION](VALIDATION.md) 及对应 PR 为准，不能由此关闭 [TODO](TODO.md) 的真人、物理与长时验收。
