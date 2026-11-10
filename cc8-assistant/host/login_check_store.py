"""Bounded owner-private login receipts; preserve terminal attempts before rearming."""

from pathlib import Path

from private_output import PrivateOutput, private_directory, read_private_json
from process_lock import ProcessLease, atomic_private_json
from login_receipt import document, require


class LoginCheckStore:
    def __init__(self, private):
        self.private = Path(private)
        self.path = self.private / 'login-check.json'

    def load(self):
        try:
            return document(read_private_json(self.path, 32768))
        except FileNotFoundError:
            require(not self.path.is_symlink(), 'login_check_linked')
            return None

    def arm(self, value):
        document(value)
        require(value['state'] == 'armed', 'login_check_invalid')
        private_directory(self.private)
        with ProcessLease(self.private / 'login-check.lock'):
            old = self.load()
            if old is not None:
                require(old['state'] not in ('armed', 'observing'), 'login_check_already_pending')
                folder = private_directory(self.private / 'login-check-history')
                archive = folder / ('login-' + old['id'] + '.json')
                if archive.exists() or archive.is_symlink():
                    require(read_private_json(archive, 32768) == old, 'login_history_conflict')
                else:
                    require(len(list(folder.iterdir())) < 16, 'login_history_capacity')
                    with PrivateOutput(archive) as output:
                        output.write(old)
            atomic_private_json(self.path, value)

    def replace(self, ident, value, expected='observing'):
        document(value)
        require(value['id'] == ident, 'login_check_replaced')
        private_directory(self.private)
        with ProcessLease(self.private / 'login-check.lock'):
            current = self.load()
            if current is None or current['id'] != ident or current['state'] != expected:
                return False
            atomic_private_json(self.path, value)
            return True
