"""Private, disposable memory for provider fixtures; never read owner preferences."""

from pathlib import Path
import tempfile
import weakref
from preference_memory import PreferenceMemory


def isolated_memory():
    temporary = tempfile.TemporaryDirectory(prefix='cc8-preferences-test-')
    memory = PreferenceMemory(Path(temporary.name) / 'preferences.json')
    # Explicit cleanup precedes TemporaryDirectory's warning finalizer, including
    # helper TestCases instantiated outside run(). Neither root is an owner path.
    memory._test_cleanup = weakref.finalize(memory, temporary.cleanup)
    return memory
