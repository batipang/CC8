"""Fixed app-server peer for the UI failure runner. No Codex installation is used."""

from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys

THREAD = 'qa-thread'
BODY = 'qa-injected-sensitive-body'
CODES = {
    'timeout': {'httpConnectionFailed': {'httpStatusCode': 504}},
    'rate_limit': 'rateLimitExceeded',
    'quota': 'usageLimitExceeded',
    'credential_or_permission': 'unauthorized',
    'approval_declined': None,
}
PROMPT = re.compile(
    r'(自动故障测试|自动恢复检查) ((asr|brain|tts)-'
    r'(timeout|rate_limit|quota|credential_or_permission|transport_or_io|approval_declined)-[1-3])'
)


class LocalDot:
    def __init__(self, folder):
        from dot_client import CodexRpc, DotBrain

        self.audit = folder / 'dot-audit'
        self.audit.touch(mode=0o600)
        self.processes = []

        def spawn(command, **kwargs):
            process = subprocess.Popen(
                command, **{**kwargs, 'env': {'PATH': '/usr/bin:/bin'}, 'cwd': folder}
            )
            self.processes.append(process)
            return process

        def rpc():
            return CodexRpc(
                command=[
                    sys.executable,
                    '-I',
                    '-S',
                    str(Path(__file__).resolve()),
                    str(self.audit),
                ],
                popen=spawn,
            )

        self.brain = DotBrain(rpc())
        # Exercise the normal reconnect branch with another fixed peer, never Codex.
        self.brain.rpc_factory = rpc

    def counts(self):
        return Counter(self.audit.read_text(encoding='ascii').splitlines())

    def close(self):
        self.brain.close()
        for process in self.processes:
            if process.poll() is None:
                raise RuntimeError('Dot fixture guardian cleanup unconfirmed')
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


def peer(audit):
    def send(value):
        print(json.dumps(value), flush=True)

    def record(entry):
        with audit.open('a', encoding='ascii') as output:
            output.write(entry + '\n')

    incoming = iter(sys.stdin.buffer)
    turns = 0
    for line in incoming:
        request = json.loads(line)
        method = request.get('method')
        if method == 'initialized' and 'id' not in request:
            continue
        ident = request['id']
        if method in ('initialize', 'turn/interrupt'):
            send({'id': ident, 'result': {}})
            continue
        if method == 'thread/start':
            send({'id': ident, 'result': {'thread': {'id': THREAD}}})
            continue
        if method != 'turn/start':
            raise ValueError('Unexpected Dot fixture request')
        (prompt,) = request['params']['input']
        match = PROMPT.fullmatch(prompt['text'].rsplit('\n', 1)[-1])
        if request['params']['threadId'] != THREAD or match is None:
            raise ValueError('Unexpected Dot fixture input')
        action, case, stage, kind = match.groups()
        recovery = action == '自动恢复检查'
        key = case + (':recovery' if recovery else ':fault')
        record(key)
        failing = stage == 'brain' and not recovery
        if failing and kind == 'transport_or_io':
            # Lose the real stdio connection before turn/start is acknowledged.
            return
        turns += 1
        turn = f'qa-turn-{turns}'
        scope = {'threadId': THREAD, 'turnId': turn}
        error = None
        if failing and kind == 'approval_declined':
            approval = 'qa-approval-' + turn
            send(
                {
                    'id': approval,
                    'method': 'item/commandExecution/requestApproval',
                    'params': {**scope, 'itemId': 'qa-command', 'command': BODY},
                }
            )
            reply = json.loads(next(incoming))
            if reply != {'id': approval, 'result': {'decision': 'decline'}}:
                raise ValueError('Dot fixture approval was not declined')
            record(case + ':declined')
        elif failing:
            error = {'message': BODY, 'additionalDetails': BODY, 'codexErrorInfo': CODES[kind]}
        send({'id': ident, 'result': {'turn': {'id': turn}}})
        if not failing:
            send(
                {
                    'method': 'item/completed',
                    'params': {
                        **scope,
                        'item': {
                            'id': 'qa-answer-' + turn,
                            'type': 'agentMessage',
                            'phase': 'final_answer',
                            'text': '自动测试恢复检查已完成。',
                        },
                    },
                }
            )
        send(
            {
                'method': 'turn/completed',
                'params': {
                    'threadId': THREAD,
                    'turn': {
                        'id': turn,
                        'status': 'failed' if failing else 'completed',
                        'error': error,
                    },
                },
            }
        )


if __name__ == '__main__':
    peer(Path(sys.argv[1]))
