"""Explicit Finder location of a receipt-owned regular file; no content open."""

from pathlib import Path
import stat
import subprocess
import sys
from result_provenance import validate


def reveal_file(record, index, *, run=subprocess.run):
    """Only locate an actual receipt's existing regular file; never execute it."""
    if sys.platform != 'darwin':
        raise OSError('Finder location requires the connected Mac')
    value = validate(record.get('provenance'))
    if type(index) is not int or not 0 <= index < len(value['files']):
        raise ValueError('Unknown result file')
    item = value['files'][index]
    if item['status'] != 'completed' or item['kind'] == 'delete':
        raise ValueError('Result file operation was not a completed write')
    path = Path(item['move_to'] or item['path'])
    # lstat rejects a symlink at the selected file itself. Parent-directory
    # aliases such as macOS /var -> /private/var remain usable; revealing an
    # existing file is not a content read or a claim about its current contents.
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('Result file changed or is not regular')
    # Absolute path is one argv operand, including spaces or shell punctuation.
    # Finder reveal also safely handles executable/document files without opening.
    result = run(['/usr/bin/open', '-R', str(path)], capture_output=True, timeout=5)
    if result.returncode:
        raise OSError('Finder did not confirm the reveal request')
