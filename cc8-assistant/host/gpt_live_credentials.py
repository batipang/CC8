"""Read the optional Mac-side Live API key without loading the audio stack."""

import os
from pathlib import Path
import re

from runtime_guard import _private_file

KEY_FILE = '.toolchains/cc8/openai-api-key'


def load_key(path: Path) -> str:
    value = os.environ.get('OPENAI_API_KEY')
    if value is None:
        value = _private_file(path, 1024).decode().strip()
    if not re.fullmatch(r'[\x21-\x7e]{16,512}', value):
        raise ValueError('Invalid OpenAI API credential')
    return value
