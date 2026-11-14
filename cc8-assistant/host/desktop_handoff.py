"""Open the original local Codex chat bound to a saved handoff."""

import subprocess
import sys

from source_identity import identifier


def can_open_chat(thread_id):
    return sys.platform == 'darwin' and identifier(thread_id).lower() != 'new'


def launch_chat(thread_id):
    if not can_open_chat(thread_id):
        raise ValueError('Cannot open this local chat')
    return subprocess.Popen(
        ['/usr/bin/open', '-b', 'com.openai.codex', 'codex://threads/' + thread_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def confirm_open(process):
    try:
        status = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    if status:
        raise OSError('Mac did not accept the open request')
