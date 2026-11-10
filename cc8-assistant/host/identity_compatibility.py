"""Prevent an old backend from ignoring a persisted guest privacy boundary."""

import ast
from pathlib import Path

from identity_mode import IdentityModeStore


def storage_gate(before_root, after_root):
    store = IdentityModeStore(
        Path(before_root) / 'device-access/private/voice-runtime/identity-mode.json'
    )
    state = store.snapshot()
    source = Path(after_root) / 'cc8-assistant/host/bridge.py'
    supported = False
    if source.is_file() and not source.is_symlink():
        for node in ast.parse(source.read_text()).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(n, ast.Name) and n.id == 'IDENTITY_READER_VERSION' for n in node.targets
            ):
                supported = (
                    isinstance(node.value, ast.Constant)
                    and type(node.value.value) is int
                    and node.value.value == 1
                )
    target = IdentityModeStore(
        Path(after_root) / 'device-access/private/voice-runtime/identity-mode.json'
    )
    target_state = target.snapshot()
    source_present, target_present = store.path.exists(), target.path.exists()
    same_root = Path(before_root).resolve() == Path(after_root).resolve()
    preserved = state == target_state and (not source_present or target_present)
    compatible = state.mode == 'owner' or supported
    if not same_root and (
        target_present and target_state != state or supported and source_present and not preserved
    ):
        # Reader compatibility cannot silently replace a missing handoff or
        # an independently selected target role. This check never copies data.
        compatible = False
    return {
        'mode': state.mode,
        'store_revision': state.revision,
        'reader_supported': supported,
        'state_preserved': preserved,
        'compatible': compatible,
        'content_exported': False,
    }
