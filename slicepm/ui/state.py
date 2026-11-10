"""What the window shows: the disks as they are, plus the changes waiting to be applied."""
from .. import ops, scan
from ..model import OpError


class State:
    def __init__(self):
        self.images = []          # disk image paths opened by the user
        self.baseline = {}        # path -> Disk, as read from the disks
        self.pending = []         # operation dicts, in order
        self.disks = {}           # baseline with every pending operation applied
        self.error = None

    def rescan(self):
        self.error = None
        found = {}
        try:
            for d in scan.scan_devices():
                found[d.path] = d
        except Exception as e:  # lsblk missing or failing
            self.error = str(e)
        for path in list(self.images):
            try:
                found[path] = scan.scan_image(path)
            except OSError:
                self.images.remove(path)
        self.baseline = found
        self.pending = []
        self.disks = {p: d.clone() for p, d in found.items()}

    def add_image(self, path):
        if path not in self.images:
            self.images.append(path)
        self.rescan_keep_pending()

    def rescan_keep_pending(self):
        pending = self.pending
        self.rescan()
        self.pending = []
        for o in pending:
            try:
                self.queue(o)
            except OpError:
                break

    def queue(self, o):
        """Add an operation, or raise OpError and leave everything unchanged."""
        trial = {p: d.clone() for p, d in self.disks.items()}
        o = dict(o)
        o['desc'] = ops.describe(o, self.disks)
        ops.simulate(trial, o)
        self.pending.append(o)
        self.disks = trial

    def undo(self):
        if self.pending:
            self.pending.pop()
            self.disks = ops.replay(self.baseline, self.pending)

    def discard(self):
        self.pending = []
        self.disks = {p: d.clone() for p, d in self.baseline.items()}

    def touched(self):
        out = set()
        for o in self.pending:
            out |= ops.touches_disks(o)
        return out

    def uses_images(self):
        return any(self.baseline[p].is_image for p in self.touched() if p in self.baseline)
