"""Fixed recovery guidance; never interpret exception text as UI or instructions.

No action here retries a task or opens capture. Brain failures may follow partial
side effects, so recovery asks the owner to inspect results before resubmitting.
"""

from dataclasses import dataclass
from typing import Protocol
from operation_failure import STAGES, KINDS

LABELS = {
    'asr': '语音识别',
    'brain': '助理执行',
    'tts': '语音合成',
    'playback': '设备播放',
    'transport': '设备连接',
    'runtime': '助手运行',
}
REASONS = {
    'rate_limit': '服务限流',
    'quota': '服务额度不足',
    'credential_or_permission': '凭证或服务权限不可用',
    'approval_declined': '需要审批的操作本次未执行',
    'timeout': '请求超时',
    'transport_or_io': '连接或本机读写失败',
    'provider_or_runtime': '服务或本机处理失败',
}
RECOVERY = {
    'asr': '这句话未交给助理；检查服务后请再说一次。',
    'brain': '执行结果可能不完整；请在电脑端核对已执行步骤，再决定是否重新发起。',
    'tts': '请查看任务页的执行结果；不要重复交办已完成的任务。',
    'playback': '请检查 CC8 连接；任务执行结果以任务页为准。',
    'transport': '请检查 USB 或已配对 Wi-Fi；重连后需手动开始语音。',
    'runtime': '请查看电脑端诊断；本次不会自动重试任务。',
}


@dataclass(frozen=True)
class FailureFeedback:
    stage: str
    kind: str
    message: str


def feedback(stage, kind):
    if stage not in STAGES or kind not in KINDS:
        raise ValueError('Invalid feedback category')
    recovery = RECOVERY[stage]
    if kind == 'approval_declined':
        recovery = '请在电脑端核对已执行步骤和权限后重新发起；当前没有待批准的请求。'
    elif kind == 'quota':
        recovery = '请在电脑端检查对应服务额度。' + recovery
    elif kind == 'credential_or_permission':
        recovery = '请在电脑端检查对应服务凭证和权限。' + recovery
    return FailureFeedback(stage, kind, LABELS[stage] + ' · ' + REASONS[kind] + '。' + recovery)


class FailureSink(Protocol):
    def __call__(
        self, message: str, error: BaseException, *, trace_key: str | int | None = None
    ) -> None: ...
