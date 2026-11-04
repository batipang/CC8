"""Compatibility exports for the shared owner-private report writer."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.private_output import PrivateOutput, private_directory, read_private_json  # noqa: E402

__all__ = ['PrivateOutput', 'private_directory', 'read_private_json']
