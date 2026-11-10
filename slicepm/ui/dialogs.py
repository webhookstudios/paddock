"""Small forms for each action. Each returns the operation to queue, or None if cancelled."""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk

from .. import fs as fsinfo
from .. import ops
from ..model import MiB, GiB, human


def _spin(title, subtitle, value, lower, upper, step=1):
    adj = Gtk.Adjustment(value=value, lower=lower, upper=max(lower, upper), step_increment=step,
                         page_increment=max(step, 100))
    row = Adw.SpinRow(title=title, subtitle=subtitle, adjustment=adj, digits=0)
    return row


def _combo(title, items, selected=0):
    row = Adw.ComboRow(title=title, model=Gtk.StringList.new(items))
    row.set_selected(selected)
    return row


def _group(*rows):
    g = Adw.PreferencesGroup()
    for r in rows:
        g.add(r)
    return g


def ask(window, heading, body, content, ok, appearance='suggested', on_ok=None):
    dlg = Adw.AlertDialog(heading=heading, body=body)
    if content is not None:
        dlg.set_extra_child(content)
    dlg.add_response('cancel', 'Cancel')
    dlg.add_response('ok', ok)
    dlg.set_response_appearance('ok', Adw.ResponseAppearance.DESTRUCTIVE if appearance == 'destructive'
                                else Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response('cancel' if appearance == 'destructive' else 'ok')
    dlg.set_close_response('cancel')

    def done(d, res):
        if d.choose_finish(res) == 'ok' and on_ok:
            on_ok()
    dlg.choose(window, None, done)
    return dlg


def fs_choices(include_none=True):
    keys = fsinfo.creatable()
    names = [fsinfo.FILESYSTEMS[k]['title'] for k in keys]
    if include_none:
        return [None] + keys, ['Unformatted'] + names
    return keys, names


# ---------------------------------------------------------------------------

def create_partition(window, disk, start, size, queue):
    region_mib = size // MiB
    before = _spin('Space before', 'MiB left empty ahead of the partition', 0, 0, region_mib - 1)
    sz = _spin('Size', f'MiB. Up to {human(size)} is free here', region_mib, 1, region_mib)
    keys, names = fs_choices()
    fs = _combo('Filesystem', names, keys.index('ext4') if 'ext4' in keys else 0)
    label = Adw.EntryRow(title='Label')
    esp = Adw.SwitchRow(title='EFI system partition', subtitle='Mark it as the boot partition for UEFI')

    def keep_fit(*_):
        sz.get_adjustment().set_upper(max(1, region_mib - int(before.get_value())))
        before.get_adjustment().set_upper(max(0, region_mib - int(sz.get_value())))
    before.connect('notify::value', keep_fit)
    sz.connect('notify::value', keep_fit)

    def ok():
        queue(ops.op('create', disk=disk.path, start=start + int(before.get_value()) * MiB,
                     size=int(sz.get_value()) * MiB, fs=keys[fs.get_selected()],
                     label=fsinfo.clean_label(keys[fs.get_selected()], label.get_text()),
                     flags=['esp'] if esp.get_active() else []))
    ask(window, 'New partition', f'On {disk.title()}',
        _group(before, sz, fs, label, esp), 'Add to changes', on_ok=ok)


def resize_partition(window, disk, part, queue):
    lo, hi = disk.min_start_for(part), disk.max_end_for(part)
    span = (hi - lo) // MiB
    cur_before = (part.start - lo) // MiB
    cur_size = part.size // MiB
    min_size = 1
    if part.used is not None:
        min_size = max(1, int(part.used * 1.05 // MiB) + 16)
    before = _spin('Space before', 'MiB. Change this to move the partition', cur_before, 0, span - cur_size)
    sz = _spin('Size', f'MiB. Between {min_size} and {span}', cur_size, min_size, span)
    note = Adw.ActionRow(title='Moving copies every byte, so it can take a long time.',
                         css_classes=['dim-label'])

    def keep_fit(*_):
        sz.get_adjustment().set_upper(max(min_size, span - int(before.get_value())))
        before.get_adjustment().set_upper(max(0, span - int(sz.get_value())))
    before.connect('notify::value', keep_fit)
    sz.connect('notify::value', keep_fit)

    def ok():
        ns = lo + int(before.get_value()) * MiB
        nz = int(sz.get_value()) * MiB
        queue(ops.op('resize', disk=disk.path, start=part.start, size=part.size, new_start=ns, new_size=nz))
    ask(window, 'Resize or move', f'{part.title()} on {disk.title()}',
        _group(before, sz, note), 'Add to changes', on_ok=ok)


def format_partition(window, disk, part, queue):
    keys, names = fs_choices(include_none=False)
    fs = _combo('Filesystem', names)
    label = Adw.EntryRow(title='Label', text=part.label or '')

    def ok():
        k = keys[fs.get_selected()]
        queue(ops.op('format', disk=disk.path, start=part.start, fs=k, label=fsinfo.clean_label(k, label.get_text())))
    ask(window, 'Format partition', f'Everything on {part.title()} will be erased.',
        _group(fs, label), 'Add to changes', 'destructive', ok)


def set_label(window, disk, part, queue):
    label = Adw.EntryRow(title='Label', text=part.label or '')
    ask(window, 'Change label', part.title(), _group(label), 'Add to changes',
        on_ok=lambda: queue(ops.op('label', disk=disk.path, start=part.start, label=label.get_text().strip())))


def set_flags(window, disk, part, queue):
    rows, esp, boot = [], Adw.SwitchRow(title='EFI system partition',
                                        subtitle='Used by UEFI firmware to start the computer'), None
    esp.set_active('esp' in part.flags)
    rows.append(esp)
    if disk.table == 'msdos':
        boot = Adw.SwitchRow(title='Boot flag', subtitle='Marks the partition a BIOS starts from')
        boot.set_active('boot' in part.flags)
        rows.append(boot)

    def ok():
        flags = []
        if esp.get_active():
            flags.append('esp')
        if boot is not None and boot.get_active():
            flags.append('boot')
        queue(ops.op('flags', disk=disk.path, start=part.start, flags=flags))
    ask(window, 'Flags', part.title(), _group(*rows), 'Add to changes', on_ok=ok)


def copy_partition(window, disks, disk, part, queue):
    targets = []
    for d in disks.values():
        if d.table is None or d.readonly or d.is_image != disk.is_image:
            continue
        for start, size in d.free_regions():
            if size >= part.size:
                targets.append((d, start, size))
    if not targets:
        ask(window, 'Nowhere to copy to',
            f'No free space of at least {human(part.size)} was found on a disk with a partition table.',
            None, 'OK')
        return
    names = [f'{d.title()}: {human(s)} free at {human(st)}' for d, st, s in targets]
    where = _combo('Copy to', names)
    sz = _spin('Size of the copy', 'MiB. Larger copies grow the filesystem if it can', part.size // MiB,
               part.size // MiB, targets[0][2] // MiB)

    def changed(*_):
        _, _, room = targets[where.get_selected()]
        sz.get_adjustment().set_upper(room // MiB)
        sz.set_value(min(max(sz.get_value(), part.size // MiB), room // MiB))
    where.connect('notify::selected', changed)

    def ok():
        d, st, _ = targets[where.get_selected()]
        queue(ops.op('copy', disk=d.path, src_disk=disk.path, src_start=part.start, start=st,
                     size=int(sz.get_value()) * MiB))
    ask(window, 'Copy partition', f'{part.title()} ({human(part.size)})', _group(where, sz),
        'Add to changes', on_ok=ok)


def new_table(window, disk, queue):
    kind = _combo('Partition table', ['GPT (recommended)', 'MBR (older BIOS systems)'])
    ask(window, 'New partition table',
        f'This erases every partition on {disk.title()}.', _group(kind), 'Add to changes', 'destructive',
        lambda: queue(ops.op('create_table', disk=disk.path, table=['gpt', 'msdos'][kind.get_selected()])))


def convert_table(window, disk, queue):
    to = 'msdos' if disk.table == 'gpt' else 'gpt'
    ask(window, f'Convert to {to.upper() if to == "gpt" else "MBR"}',
        'The partitions stay as they are. Make sure you have a backup, and fix the boot loader afterwards '
        'if this disk starts your computer.', None, 'Add to changes', 'destructive',
        lambda: queue(ops.op('convert', disk=disk.path, table=to)))


def wipe_disk(window, disk, queue):
    keys = list(ops.WIPE_MODES)
    mode = _combo('Method', [ops.WIPE_MODES[k] for k in keys])
    ask(window, 'Wipe disk', f'Everything on {disk.title()} will be lost for good.', _group(mode),
        'Add to changes', 'destructive',
        lambda: queue(ops.op('wipe', disk=disk.path, mode=keys[mode.get_selected()])))


def clone_disk(window, disks, target, queue):
    sources = [d for d in disks.values() if d.path != target.path and d.size <= target.size
               and d.is_image == target.is_image and d.table]
    if not sources:
        ask(window, 'No disk to clone',
            f'Another disk with a partition table, no bigger than {human(target.size)}, is needed.', None, 'OK')
        return
    src = _combo('Copy from', [d.title() + f' · {human(d.size)}' for d in sources])
    ask(window, 'Clone a disk',
        f'Everything on {target.title()} will be replaced by an exact copy of the source.', _group(src),
        'Add to changes', 'destructive',
        lambda: queue(ops.op('clone', disk=target.path, src=sources[src.get_selected()].path)))
