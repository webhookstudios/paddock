"""Plain data model for disks and partitions, plus free-space maths.

Everything is in bytes. Nothing here touches a disk.
"""
import copy
from dataclasses import dataclass, field

MiB = 1 << 20
GiB = 1 << 30

GPT_LINUX = '0fc63daf-8483-4772-8e79-3d69d8477de4'
GPT_MSDATA = 'ebd0a0a2-b9e5-4433-87c0-68b6b72699c7'
GPT_SWAP = '0657fd6d-a4ab-43c4-84e5-0933c84b4f4f'
GPT_ESP = 'c12a7328-f81f-11d2-ba4b-00a0c93ec93b'

MBR_EXTENDED = {'0x05', '0x0f', '0x85', '5', '0f', '85', 'f'}


class OpError(Exception):
    """A change that cannot be made. The message is shown to the user."""


@dataclass
class Partition:
    start: int
    size: int
    fs: str | None = None          # 'ext4', 'ntfs', 'vfat', ... or None when unformatted
    label: str = ''
    ptype: str = ''                # GPT type GUID or MBR type like '0x83'
    number: int | None = None      # partition number on the disk, None while still pending
    node: str | None = None        # /dev/sda1
    uuid: str = ''
    mountpoint: str | None = None
    used: int | None = None        # bytes, when known
    flags: set = field(default_factory=set)   # 'esp', 'boot'
    role: str = 'primary'          # 'primary', 'extended', 'logical'
    new: bool = False              # created in this session, not on the disk yet
    changed: bool = False          # modified in this session

    @property
    def end(self):
        return self.start + self.size

    @property
    def editable(self):
        return self.role == 'primary'

    @property
    def mounted(self):
        return bool(self.mountpoint)

    def title(self):
        if self.node:
            return self.node.rsplit('/', 1)[-1]
        if self.number and not self.new:
            return f'Partition {self.number}'
        return 'New partition'


@dataclass
class Disk:
    path: str
    size: int
    name: str = ''
    table: str | None = None       # 'gpt', 'msdos' or None for a blank disk
    sector: int = 512
    removable: bool = False
    transport: str = ''
    rotational: bool | None = None
    readonly: bool = False
    is_image: bool = False
    parts: list = field(default_factory=list)

    def clone(self):
        return copy.deepcopy(self)

    def title(self):
        base = self.path.rsplit('/', 1)[-1]
        return f'{self.name} ({base})' if self.name else base

    # --- layout maths -----------------------------------------------------

    @property
    def usable_start(self):
        return MiB

    @property
    def usable_end(self):
        """First byte past the area partitions may use, keeping the GPT backup clear."""
        if self.table == 'gpt' or self.table is None:
            return max(0, (self.size // MiB - 1)) * MiB
        return (self.size // MiB) * MiB

    def sorted_parts(self):
        return sorted(self.parts, key=lambda p: p.start)

    def free_regions(self, minimum=MiB):
        """List of (start, size) gaps big enough to hold a partition."""
        regions = []
        cursor = self.usable_start
        for p in self.sorted_parts():
            if p.role == 'logical':
                continue
            gap_start = _align_up(cursor)
            if p.start - gap_start >= minimum:
                regions.append((gap_start, _align_down(p.start) - gap_start))
            cursor = max(cursor, p.end)
        gap_start = _align_up(cursor)
        if self.usable_end - gap_start >= minimum:
            regions.append((gap_start, self.usable_end - gap_start))
        return [(s, z) for s, z in regions if z >= minimum]

    def find(self, start):
        for p in self.parts:
            if p.start == start:
                return p
        raise OpError('That partition no longer exists.')

    def primary_count(self):
        return sum(1 for p in self.parts if p.role in ('primary', 'extended'))

    def max_end_for(self, part):
        """Furthest the partition could grow, counting free space right after it."""
        later = [p.start for p in self.parts if p.start >= part.end and p is not part and p.role != 'logical']
        limit = min(later) if later else self.usable_end
        return min(limit, self.usable_end)

    def min_start_for(self, part):
        earlier = [p.end for p in self.parts if p.end <= part.start and p is not part and p.role != 'logical']
        return max(earlier) if earlier else self.usable_start


def _align_up(n):
    return -(-n // MiB) * MiB


def _align_down(n):
    return n // MiB * MiB


def align_up(n):
    return _align_up(n)


def align_down(n):
    return _align_down(n)


def human(n):
    if n is None:
        return '—'
    n = float(n)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'.replace('.0 ', ' ')
        n /= 1024
    return f'{n:.1f} TiB'


def ptype_for(fs, table, esp=False):
    if table == 'gpt':
        if esp:
            return GPT_ESP
        if fs == 'swap':
            return GPT_SWAP
        if fs in ('ntfs', 'exfat', 'vfat'):
            return GPT_MSDATA
        return GPT_LINUX
    if esp:
        return '0xef'
    return {'swap': '0x82', 'ntfs': '0x07', 'exfat': '0x07', 'vfat': '0x0c'}.get(fs, '0x83')
