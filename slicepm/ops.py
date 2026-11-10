"""Pending operations: what to do, how to show it, and how it changes the layout.

An operation is a small dict (so it can be sent to the privileged helper as JSON).
`simulate` applies it to the in-memory disks so the map updates before anything is written.
Partitions are identified by (disk, start byte), which stays unique on a disk.
"""
from . import fs as fsinfo
from .model import (MiB, OpError, Partition, align_up, align_down, human, ptype_for,
                    GPT_ESP, GPT_MSDATA)

KINDS = ('create_table', 'create', 'delete', 'format', 'resize', 'label', 'flags',
         'check', 'copy', 'wipe', 'clone', 'convert')

WIPE_MODES = {'quick': 'Quick (erase signatures)', 'zero': 'Overwrite with zeros',
              'random': 'Overwrite with random data'}


def op(kind, **args):
    assert kind in KINDS, kind
    return dict(kind=kind, **args)


def _part(disks, o, key='start'):
    d = disks[o['disk']]
    return d, d.find(o[key])


def _need_unmounted(p):
    if p.mounted:
        raise OpError(f'{p.title()} is mounted at {p.mountpoint}. Unmount it first.')


def _need_editable(p):
    if not p.editable:
        raise OpError('Slice cannot change extended or logical MBR partitions yet.')


def _mounted_anywhere(d):
    for p in d.parts:
        if p.mounted:
            return p
    return None


def check_label(fs, label):
    info = fsinfo.FILESYSTEMS.get(fs or '')
    if info and len(label) > info['label']:
        raise OpError(f'{info["title"]} labels can be at most {info["label"]} characters.')


# ---------------------------------------------------------------------------

def describe(o, disks):
    k = o['kind']
    d = disks.get(o.get('disk'))
    name = d.title() if d else '?'
    if k == 'create_table':
        return f'Create a new {o["table"].upper()} partition table on {name}. This erases every partition on it.'
    if k == 'create':
        fs = fsinfo.FILESYSTEMS.get(o.get('fs') or '', {}).get('title', 'unformatted')
        label = f' "{o["label"]}"' if o.get('label') else ''
        return f'Create a {human(o["size"])} {fs} partition{label} on {name}.'
    if k == 'delete':
        return f'Delete the partition at {human(o["start"])} on {name}.'
    if k == 'format':
        fs = fsinfo.FILESYSTEMS.get(o['fs'], {}).get('title', o['fs'])
        return f'Format the partition at {human(o["start"])} on {name} as {fs}. Its data is erased.'
    if k == 'resize':
        at = human(o['start'])
        moving, sizing = o['new_start'] != o['start'], o['new_size'] != o['size']
        if moving and sizing:
            return (f'Move the partition at {at} on {name} to {human(o["new_start"])} '
                    f'and resize it to {human(o["new_size"])}.')
        if moving:
            return f'Move the partition at {at} on {name} to {human(o["new_start"])}.'
        return f'Resize the partition at {at} on {name} to {human(o["new_size"])}.'
    if k == 'label':
        return f'Set the label of the partition at {human(o["start"])} on {name} to "{o["label"]}".'
    if k == 'flags':
        return f'Set flags {sorted(o["flags"]) or "(none)"} on the partition at {human(o["start"])} on {name}.'
    if k == 'check':
        return f'Check and repair the filesystem at {human(o["start"])} on {name}.'
    if k == 'copy':
        src = disks.get(o['src_disk'])
        return (f'Copy the partition at {human(o["src_start"])} on {src.title() if src else "?"} '
                f'to {human(o["start"])} on {name}.')
    if k == 'wipe':
        return f'Wipe {name}: {WIPE_MODES[o["mode"]].lower()}. Everything on it is lost.'
    if k == 'clone':
        src = disks.get(o['src'])
        return f'Clone {src.title() if src else "?"} onto {name}. Everything on {name} is replaced.'
    if k == 'convert':
        return f'Convert the partition table on {name} to {o["table"].upper()}.'
    return k


def is_destructive(o):
    return o['kind'] in ('create_table', 'delete', 'format', 'wipe', 'clone', 'resize', 'convert', 'copy')


def touches_disks(o):
    keys = ('disk', 'src_disk', 'src')
    return {o[k] for k in keys if k in o}


# ---------------------------------------------------------------------------

def simulate(disks, o):
    """Apply one operation to the in-memory disks, or raise OpError."""
    k = o['kind']
    if k == 'create_table':
        d = disks[o['disk']]
        if o['table'] not in ('gpt', 'msdos'):
            raise OpError('Pick GPT or MBR.')
        if _mounted_anywhere(d):
            raise OpError(f'{_mounted_anywhere(d).title()} is mounted. Unmount everything on the disk first.')
        d.table = o['table']
        d.parts = []
    elif k == 'create':
        _create(disks, o)
    elif k == 'delete':
        d, p = _part(disks, o)
        _need_editable(p)
        _need_unmounted(p)
        d.parts.remove(p)
    elif k == 'format':
        d, p = _part(disks, o)
        _need_editable(p)
        _need_unmounted(p)
        if not fsinfo.available(o['fs']):
            raise OpError(f'The tool to make {o["fs"]} filesystems is not installed.')
        if o['fs'] == 'vfat' and p.size < 33 * MiB:
            raise OpError('FAT32 needs a partition of at least 33 MiB.')
        check_label(o['fs'], o.get('label', ''))
        p.fs, p.label, p.used, p.changed = o['fs'], o.get('label', ''), None, True
        p.ptype = ptype_for(o['fs'], d.table, 'esp' in p.flags)
    elif k == 'resize':
        _resize(disks, o)
    elif k == 'label':
        d, p = _part(disks, o)
        _need_editable(p)
        if not fsinfo.can_label(p.fs) and p.new is False:
            raise OpError('Slice cannot set a label on this kind of filesystem.')
        check_label(p.fs, o['label'])
        p.label, p.changed = o['label'], True
    elif k == 'flags':
        d, p = _part(disks, o)
        _need_editable(p)
        flags = set(o['flags'])
        if 'boot' in flags and d.table != 'msdos':
            raise OpError('The boot flag only exists on MBR disks. Use the EFI flag on GPT.')
        if 'esp' in flags:
            p.ptype = GPT_ESP if d.table == 'gpt' else '0xef'
        elif 'esp' in p.flags:
            p.ptype = ptype_for(p.fs, d.table)
        p.flags, p.changed = flags, True
    elif k == 'check':
        d, p = _part(disks, o)
        _need_editable(p)
        _need_unmounted(p)
        if not fsinfo.can_check(p.fs):
            raise OpError('Slice has no checking tool for this filesystem.')
    elif k == 'copy':
        _copy(disks, o)
    elif k == 'wipe':
        d = disks[o['disk']]
        if o['mode'] not in WIPE_MODES:
            raise OpError('Unknown wipe mode.')
        if _mounted_anywhere(d):
            raise OpError(f'{_mounted_anywhere(d).title()} is mounted. Unmount everything on the disk first.')
        d.table, d.parts = None, []
    elif k == 'clone':
        _clone(disks, o)
    elif k == 'convert':
        _convert(disks, o)
    else:
        raise OpError(f'Unknown operation {k}.')


def _create(disks, o):
    d = disks[o['disk']]
    if d.table is None:
        raise OpError('This disk has no partition table yet. Create one first.')
    start, size = o['start'], o['size']
    if start % MiB or size % MiB:
        raise OpError('Partitions are aligned to 1 MiB.')
    if size < MiB:
        raise OpError('That partition is too small.')
    if not any(rs <= start and start + size <= rs + rz for rs, rz in d.free_regions()):
        raise OpError('That does not fit in the free space.')
    if d.table == 'msdos' and d.primary_count() >= 4:
        raise OpError('An MBR disk holds at most four primary partitions. Use GPT for more.')
    if d.table == 'gpt' and len(d.parts) >= 128:
        raise OpError('A GPT disk holds at most 128 partitions.')
    fs = o.get('fs') or None
    if fs:
        if not fsinfo.available(fs):
            raise OpError(f'The tool to make {fs} filesystems is not installed.')
        if fs == 'vfat' and size < 33 * MiB:
            raise OpError('FAT32 needs a partition of at least 33 MiB.')
        check_label(fs, o.get('label', ''))
    flags = set(o.get('flags') or [])
    d.parts.append(Partition(start=start, size=size, fs=fs, label=o.get('label', ''),
                             ptype=ptype_for(fs, d.table, 'esp' in flags), flags=flags,
                             new=True, changed=True))


def _resize(disks, o):
    d, p = _part(disks, o)
    _need_editable(p)
    _need_unmounted(p)
    ns, nz = o['new_start'], o['new_size']
    if ns % MiB or nz % MiB:
        raise OpError('Partitions are aligned to 1 MiB.')
    if nz < MiB:
        raise OpError('That partition is too small.')
    if ns < d.min_start_for(p) or ns + nz > d.max_end_for(p):
        raise OpError('That does not fit in the free space around the partition.')
    if (ns, nz) == (p.start, p.size):
        raise OpError('Nothing changed.')
    if not p.new and nz != p.size:
        reason = fsinfo.resize_reason(p.fs)
        if reason:
            raise OpError(reason)
        if nz < p.size and p.used is not None and nz < int(p.used * 1.05) + 16 * MiB:
            raise OpError(f'The files on it need about {human(p.used)}, so it cannot shrink that far.')
    if p.fs == 'vfat' and nz < 33 * MiB:
        raise OpError('FAT32 needs at least 33 MiB.')
    p.start, p.size, p.changed = ns, nz, True
    if p.used is not None and p.used > nz:
        p.used = nz


def _copy(disks, o):
    src_d = disks[o['src_disk']]
    src = src_d.find(o['src_start'])
    d = disks[o['disk']]
    _need_editable(src)
    _need_unmounted(src)
    if d.table is None:
        raise OpError('The destination disk has no partition table.')
    if src_d.is_image != d.is_image:
        raise OpError('A disk image and a real disk cannot be used in the same operation.')
    if o['size'] < src.size:
        raise OpError('The destination is smaller than the partition being copied.')
    if o['size'] > src.size and fsinfo.resize_reason(src.fs) and src.fs is not None:
        # Copies are placed at exactly the same size when the filesystem cannot grow.
        raise OpError('This filesystem cannot grow, so choose a destination of exactly the same size.')
    created = dict(o, kind='create')
    created = op('create', disk=o['disk'], start=o['start'], size=o['size'], fs=src.fs,
                 label=src.label)
    _create(disks, created)
    new = d.find(o['start'])
    new.ptype, new.fs, new.used, new.flags = src.ptype, src.fs, src.used, set(src.flags)
    if d.table != src_d.table:
        new.ptype = ptype_for(src.fs, d.table, 'esp' in src.flags)


def _clone(disks, o):
    src, dst = disks[o['src']], disks[o['disk']]
    if src.path == dst.path:
        raise OpError('Pick two different disks.')
    if src.is_image != dst.is_image:
        raise OpError('A disk image and a real disk cannot be used in the same operation.')
    if dst.size < src.size:
        raise OpError(f'{dst.title()} is smaller than {src.title()}.')
    if _mounted_anywhere(dst):
        raise OpError(f'{_mounted_anywhere(dst).title()} is mounted. Unmount everything on the target first.')
    if _mounted_anywhere(src):
        raise OpError(f'{_mounted_anywhere(src).title()} is mounted. Unmount it first so the copy is consistent.')
    new = src.clone()
    new.path, new.name, new.size = dst.path, dst.name, dst.size
    new.removable, new.transport, new.rotational, new.is_image = (
        dst.removable, dst.transport, dst.rotational, dst.is_image)
    for p in new.parts:
        p.node = p.number = None
        p.new = p.changed = True
    disks[dst.path] = new


def _convert(disks, o):
    d = disks[o['disk']]
    if d.table is None:
        raise OpError('This disk has no partition table to convert.')
    to = o['table']
    if to == d.table:
        raise OpError(f'The disk already uses {to.upper()}.')
    if _mounted_anywhere(d):
        raise OpError(f'{_mounted_anywhere(d).title()} is mounted. Unmount everything on the disk first.')
    if to == 'msdos':
        if len(d.parts) > 4:
            raise OpError('MBR holds only four primary partitions, and this disk has more.')
        if d.size > 2 * 1024 ** 4:
            raise OpError('MBR cannot address more than 2 TiB.')
        if any(p.end > 2 * 1024 ** 4 for p in d.parts):
            raise OpError('A partition lies beyond the 2 TiB MBR limit.')
    d.table = to
    for p in d.parts:
        p.ptype = ptype_for(p.fs, to, 'esp' in p.flags)
        p.changed = True
    if to == 'gpt':
        for p in d.parts:
            p.flags.discard('boot')


def replay(baseline, ops):
    """Fresh copy of the disks with every pending operation applied."""
    disks = {path: d.clone() for path, d in baseline.items()}
    for o in ops:
        simulate(disks, o)
    return disks
