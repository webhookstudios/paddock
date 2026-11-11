"""The part of Paddock that writes to disks.

It runs as root (through pkexec) for real disks, or as the user for disk image files.
It reads a JSON list of operations on stdin and prints one JSON event per line.
Everything runs as argument lists: no shell, so names with odd characters are harmless.
"""
import contextlib
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile

from . import fs as fsinfo
from .model import MiB, ptype_for

CHUNK = 4 * MiB


class StepError(Exception):
    pass


def event(**kw):
    sys.stdout.write(json.dumps(kw) + '\n')
    sys.stdout.flush()


def mounted_sources():
    """Device paths that are mounted or used as swap right now."""
    found = set()
    for src in ('/proc/self/mountinfo',):
        try:
            for line in open(src):
                parts = line.split(' - ', 1)
                if len(parts) == 2:
                    dev = parts[1].split()[1]
                    if dev.startswith('/dev/'):
                        found.add(os.path.realpath(dev))
        except OSError:
            pass
    try:
        for line in list(open('/proc/swaps'))[1:]:
            found.add(os.path.realpath(line.split()[0]))
    except OSError:
        pass
    return found


def part_node(disk, number):
    return f'{disk}p{number}' if disk[-1].isdigit() else f'{disk}{number}'


class Executor:
    def __init__(self, dry_run=False):
        self.dry = dry_run
        self.is_root = os.geteuid() == 0

    # ---- plumbing ----------------------------------------------------------

    def log(self, text):
        event(event='log', line=text)

    def run(self, argv, input=None, ok=(0,)):
        self.log('$ ' + ' '.join(argv))
        if self.dry:
            return ''
        try:
            p = subprocess.run(argv, input=input if input is not None else '', capture_output=True, text=True)
        except FileNotFoundError:
            raise StepError(f'{argv[0]} is not installed.')
        for line in (p.stdout + p.stderr).splitlines():
            if line.strip():
                self.log(line)
        if p.returncode not in ok:
            raise StepError(f'{argv[0]} failed (exit {p.returncode}).')
        return p.stdout

    def check_disk(self, disk):
        try:
            st = os.stat(disk)
        except OSError:
            raise StepError(f'{disk} does not exist.')
        if stat.S_ISBLK(st.st_mode):
            return True
        if stat.S_ISREG(st.st_mode):
            if self.is_root:
                # A root helper must never write to an arbitrary file the caller names.
                raise StepError('Disk image files are only opened without administrator rights.')
            return False
        raise StepError(f'{disk} is not a disk.')

    def disk_nodes(self, disk):
        base = disk
        found = set()
        for src in mounted_sources():
            if src == os.path.realpath(base) or re.fullmatch(re.escape(os.path.realpath(base)) + r'p?\d+', src):
                found.add(src)
        return found

    def need_unmounted_disk(self, disk):
        if self.check_disk(disk):
            busy = self.disk_nodes(disk)
            if busy:
                raise StepError(f'{sorted(busy)[0]} is in use. Unmount it first.')

    def need_unmounted_part(self, disk, number):
        if self.check_disk(disk) and number is not None:
            node = os.path.realpath(part_node(disk, number))
            if node in mounted_sources():
                raise StepError(f'{node} is in use. Unmount it first.')

    # ---- partition table helpers --------------------------------------------

    def table(self, disk):
        out = subprocess.run(['sfdisk', '-J', disk], capture_output=True, text=True)
        if out.returncode != 0 or not out.stdout.strip():
            raise StepError(f'{disk} has no readable partition table.')
        return json.loads(out.stdout)['partitiontable']

    def number_at(self, disk, start):
        t = self.table(disk)
        ss = int(t.get('sectorsize', 512))
        for p in t.get('partitions', []):
            if p['start'] * ss == start:
                m = re.search(r'(\d+)$', p['node'])
                return int(m.group(1))
        raise StepError(f'No partition starts at {start} bytes on {disk}.')

    def sector(self, disk):
        return int(self.table(disk).get('sectorsize', 512))

    def reread(self, disk):
        if self.dry:
            return
        if self.check_disk(disk):
            subprocess.run(['udevadm', 'settle'], capture_output=True)
            p = subprocess.run(['partprobe', disk], capture_output=True, text=True)
            if p.returncode != 0:
                subprocess.run(['partx', '-u', disk], capture_output=True)
                self.log('The kernel kept the old table for busy partitions. It updates after a reboot.')
            subprocess.run(['udevadm', 'settle'], capture_output=True)

    # ---- raw copying ---------------------------------------------------------

    def copy_bytes(self, src, src_off, dst, dst_off, length, label):
        """Copy `length` bytes, safe when the two ranges overlap on one disk."""
        if self.dry:
            self.log(f'(would copy {length} bytes)')
            return
        backwards = os.path.realpath(src) == os.path.realpath(dst) and dst_off > src_off
        with open(src, 'rb') as fi, open(dst, 'r+b') as fo:
            done = 0
            last = -1
            while done < length:
                n = min(CHUNK, length - done)
                pos = length - done - n if backwards else done
                fi.seek(src_off + pos)
                data = fi.read(n)
                if len(data) != n:
                    raise StepError('Read error while copying.')
                fo.seek(dst_off + pos)
                fo.write(data)
                done += n
                pct = int(done * 100 / length)
                if pct != last:
                    last = pct
                    event(event='progress', label=label, percent=pct)
            fo.flush()
            os.fsync(fo.fileno())

    def fill(self, dst, offset, length, mode, label):
        if self.dry:
            self.log(f'(would overwrite {length} bytes)')
            return
        zero = b'\0' * CHUNK
        with open(dst, 'r+b') as fo:
            fo.seek(offset)
            done, last = 0, -1
            while done < length:
                n = min(CHUNK, length - done)
                fo.write(os.urandom(n) if mode == 'random' else zero[:n])
                done += n
                pct = int(done * 100 / length)
                if pct != last:
                    last = pct
                    event(event='progress', label=label, percent=pct)
            fo.flush()
            os.fsync(fo.fileno())

    # ---- filesystem access ----------------------------------------------------

    def probe_fs(self, disk, start, size):
        out = subprocess.run(['blkid', '-p', '-O', str(start), '-S', str(size), '-o', 'export', disk],
                             capture_output=True, text=True)
        info = dict(l.split('=', 1) for l in out.stdout.splitlines() if '=' in l)
        return fsinfo.normalize(info.get('TYPE'))

    @contextlib.contextmanager
    def fs_target(self, disk, start, size, number=None):
        """A path the filesystem tools can work on.

        A real disk gives the partition node. A disk image has no nodes, so the
        partition is copied to a temporary file and written back afterwards.
        """
        if self.dry:
            yield '<partition>'
            return
        if self.check_disk(disk):
            node = part_node(disk, number if number is not None else self.number_at(disk, start))
            for _ in range(50):
                if os.path.exists(node):
                    break
                subprocess.run(['udevadm', 'settle'], capture_output=True)
                import time
                time.sleep(0.1)
            yield node
            return
        fd, tmp = tempfile.mkstemp(prefix='paddock-', suffix='.part')
        os.close(fd)
        try:
            with open(tmp, 'wb') as f:
                f.truncate(size)
            self.copy_bytes(disk, start, tmp, 0, size, 'Preparing')
            yield tmp
            os.truncate(tmp, size)      # resize2fs may have shortened the file
            self.copy_bytes(tmp, 0, disk, start, size, 'Writing back')
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    # ---- filesystem commands ---------------------------------------------------

    def mkfs(self, target, fs, label):
        info = fsinfo.FILESYSTEMS[fs]
        label = fsinfo.clean_label(fs, label)
        self.run(['wipefs', '-a', target], ok=(0, 1)) if shutil.which('wipefs') else None
        argv = list(info['mkfs'])
        if label:
            argv += [info['mkfs_label'], label]
        self.run(argv + [target])

    def shrink_fs(self, fs, target, new_size):
        kind = fsinfo.FILESYSTEMS[fs]['resize']
        if kind == 'ext':
            self.run(['e2fsck', '-f', '-y', target], ok=(0, 1))
            self.run(['resize2fs', target, f'{new_size // 1024}K'])
        elif kind == 'ntfs':
            self.run(['ntfsresize', '-f', '-s', str(new_size), target], input='y\n')

    def grow_fs(self, fs, target):
        kind = fsinfo.FILESYSTEMS[fs]['resize']
        if kind == 'ext':
            self.run(['e2fsck', '-f', '-y', target], ok=(0, 1))
            self.run(['resize2fs', target])
        elif kind == 'ntfs':
            self.run(['ntfsresize', '-f', target], input='y\n')

    # ---- the operations ----------------------------------------------------------

    def do(self, o):
        from .ops import KINDS
        if o.get('kind') not in KINDS:
            raise StepError(f'Unknown operation {o.get("kind")!r}.')
        getattr(self, 'op_' + o['kind'])(o)

    def op_create_table(self, o):
        disk = o['disk']
        self.need_unmounted_disk(disk)
        self.run(['wipefs', '-a', disk], ok=(0, 1))
        script = f'label: {"gpt" if o["table"] == "gpt" else "dos"}\n'
        self.run(['sfdisk', '--no-reread', '--force', disk], input=script)
        self.reread(disk)

    def op_create(self, o):
        disk, start, size = o['disk'], o['start'], o['size']
        self.need_unmounted_disk_space(disk)
        t = self.table(disk)
        ss = int(t.get('sectorsize', 512))
        ptype = ptype_for(o.get('fs'), 'gpt' if t['label'] == 'gpt' else 'msdos', 'esp' in (o.get('flags') or []))
        ptype = ptype.removeprefix('0x') if t['label'] != 'gpt' else ptype
        self.fill(disk, start, MiB, 'zero', 'Clearing old signatures')
        line = f'start={start // ss}, size={size // ss}, type={ptype}'
        if 'boot' in (o.get('flags') or []) and t['label'] != 'gpt':
            line += ', bootable'
        self.run(['sfdisk', '--append', '--no-reread', '--force', disk], input=line + '\n')
        self.reread(disk)
        if o.get('fs'):
            with self.fs_target(disk, start, size) as target:
                self.mkfs(target, o['fs'], o.get('label', ''))

    def need_unmounted_disk_space(self, disk):
        self.check_disk(disk)

    def op_delete(self, o):
        disk = o['disk']
        n = self.number_at(disk, o['start'])
        self.need_unmounted_part(disk, n)
        self.run(['sfdisk', '--delete', '--no-reread', disk, str(n)])
        self.reread(disk)

    def op_format(self, o):
        disk = o['disk']
        n = self.number_at(disk, o['start'])
        self.need_unmounted_part(disk, n)
        t = self.table(disk)
        ss = int(t.get('sectorsize', 512))
        size = next(p['size'] * ss for p in t['partitions'] if p['start'] * ss == o['start'])
        self.fill(disk, o['start'], MiB, 'zero', 'Clearing old signatures')
        with self.fs_target(disk, o['start'], size, n) as target:
            self.mkfs(target, o['fs'], o.get('label', ''))
        # keep the partition type in step with the filesystem
        t = t['label']
        want = ptype_for(o['fs'], 'gpt' if t == 'gpt' else 'msdos')
        self.set_type(disk, n, want)

    def set_type(self, disk, n, ptype):
        t = self.table(disk)
        want = ptype if t['label'] == 'gpt' else ptype.removeprefix('0x')
        self.run(['sfdisk', '--part-type', '--no-reread', disk, str(n), want])
        self.reread(disk)

    def rewrite_entry(self, disk, number, new_start, new_size):
        """Change one partition's start and size by editing the saved table."""
        dump = self.run_capture(['sfdisk', '-d', disk])
        ss = self.sector(disk)
        out, hit = [], False
        for line in dump.splitlines():
            m = re.match(r'^(\S+)\s*:\s*(.*)$', line)
            if m and re.search(r'(\d+)$', m.group(1)) and int(re.search(r'(\d+)$', m.group(1)).group(1)) == number:
                body = re.sub(r'start=\s*\d+', f'start={new_start // ss}', m.group(2))
                body = re.sub(r'size=\s*\d+', f'size={new_size // ss}', body)
                line = f'{m.group(1)} : {body}'
                hit = True
            out.append(line)
        if not hit:
            raise StepError('Could not find the partition in the table.')
        self.run(['sfdisk', '--no-reread', '--force', disk], input='\n'.join(out) + '\n')
        self.reread(disk)

    def run_capture(self, argv):
        p = subprocess.run(argv, capture_output=True, text=True)
        if p.returncode != 0:
            raise StepError(f'{argv[0]} failed: {p.stderr.strip()}')
        return p.stdout

    def op_resize(self, o):
        disk, start, size = o['disk'], o['start'], o['size']
        ns, nz = o['new_start'], o['new_size']
        n = self.number_at(disk, start)
        self.need_unmounted_part(disk, n)
        fs = self.probe_fs(disk, start, size)
        can = fs is not None and fsinfo.can_resize(fs)
        if nz != size and fs is not None and not can:
            raise StepError(fsinfo.resize_reason(fs) or 'This filesystem cannot be resized.')
        if nz < size and can:
            with self.fs_target(disk, start, size, n) as target:
                self.shrink_fs(fs, target, nz)
        if ns != start:
            self.copy_bytes(disk, start, disk, ns, min(size, nz), 'Moving data')
        self.rewrite_entry(disk, n, ns, nz)
        if nz > size and can:
            with self.fs_target(disk, ns, nz, n) as target:
                self.grow_fs(fs, target)

    def op_label(self, o):
        disk = o['disk']
        n = self.number_at(disk, o['start'])
        self.need_unmounted_part(disk, n)
        t = self.table(disk)
        ss = int(t.get('sectorsize', 512))
        size = next(p['size'] * ss for p in t['partitions'] if p['start'] * ss == o['start'])
        fs = self.probe_fs(disk, o['start'], size)
        info = fsinfo.FILESYSTEMS.get(fs or '')
        if not info or not info['labeler']:
            raise StepError('This filesystem cannot be relabelled.')
        label = fsinfo.clean_label(fs, o['label'])
        with self.fs_target(disk, o['start'], size, n) as target:
            if fs in ('ntfs', 'vfat', 'exfat'):
                self.run(info['labeler'] + [target, label])
            else:
                self.run(info['labeler'] + [label, target])

    def op_flags(self, o):
        disk = o['disk']
        n = self.number_at(disk, o['start'])
        t = self.table(disk)
        gpt = t['label'] == 'gpt'
        flags = set(o['flags'])
        fs = None
        ss = int(t.get('sectorsize', 512))
        size = next(p['size'] * ss for p in t['partitions'] if p['start'] * ss == o['start'])
        fs = self.probe_fs(disk, o['start'], size)
        self.set_type(disk, n, ptype_for(fs, 'gpt' if gpt else 'msdos', 'esp' in flags))
        if not gpt:
            self.run(['sfdisk', '--activate', '--no-reread', disk] + ([str(n)] if 'boot' in flags else []))
            self.reread(disk)

    def op_check(self, o):
        disk = o['disk']
        n = self.number_at(disk, o['start'])
        self.need_unmounted_part(disk, n)
        t = self.table(disk)
        ss = int(t.get('sectorsize', 512))
        size = next(p['size'] * ss for p in t['partitions'] if p['start'] * ss == o['start'])
        fs = self.probe_fs(disk, o['start'], size)
        if not fsinfo.can_check(fs):
            raise StepError('There is no checking tool for this filesystem.')
        with self.fs_target(disk, o['start'], size, n) as target:
            for cmd in fsinfo.FILESYSTEMS[fs]['check']:
                self.run(cmd + [target], ok=(0, 1))

    def op_copy(self, o):
        src_disk, dst_disk = o['src_disk'], o['disk']
        self.check_disk(src_disk)
        self.check_disk(dst_disk)
        sn = self.number_at(src_disk, o['src_start'])
        self.need_unmounted_part(src_disk, sn)
        st = self.table(src_disk)
        sss = int(st.get('sectorsize', 512))
        src = next(p for p in st['partitions'] if p['start'] * sss == o['src_start'])
        src_size = src['size'] * sss
        fs = self.probe_fs(src_disk, o['src_start'], src_size)
        dt = self.table(dst_disk)
        dss = int(dt.get('sectorsize', 512))
        gpt = dt['label'] == 'gpt'
        ptype = src['type'] if st['label'] == dt['label'] else ptype_for(fs, 'gpt' if gpt else 'msdos')
        ptype = ptype if gpt else str(ptype).removeprefix('0x')
        self.copy_bytes(src_disk, o['src_start'], dst_disk, o['start'], src_size, 'Copying partition')
        line = f'start={o["start"] // dss}, size={o["size"] // dss}, type={ptype}'
        self.run(['sfdisk', '--append', '--no-reread', '--force', dst_disk], input=line + '\n')
        self.reread(dst_disk)
        with self.fs_target(dst_disk, o['start'], o['size']) as target:
            if fs and fs.startswith('ext'):
                self.run(['tune2fs', '-U', 'random', target], ok=(0, 1))
            if o['size'] > src_size and fs and fsinfo.can_resize(fs):
                self.grow_fs(fs, target)

    def op_wipe(self, o):
        disk = o['disk']
        self.need_unmounted_disk(disk)
        size = os.path.getsize(disk) if not stat.S_ISBLK(os.stat(disk).st_mode) else self.block_size(disk)
        self.run(['wipefs', '-a', disk], ok=(0, 1))
        if o['mode'] == 'quick':
            self.fill(disk, 0, min(MiB, size), 'zero', 'Wiping start')
            if size > 2 * MiB:
                self.fill(disk, size - MiB, MiB, 'zero', 'Wiping end')
        else:
            self.fill(disk, 0, size, o['mode'], 'Wiping disk')
        self.reread(disk)

    def block_size(self, disk):
        with open(disk, 'rb') as f:
            return f.seek(0, os.SEEK_END)

    def op_clone(self, o):
        src, dst = o['src'], o['disk']
        self.check_disk(src)
        self.check_disk(dst)
        self.need_unmounted_disk(src)
        self.need_unmounted_disk(dst)
        ssize = os.path.getsize(src) if not stat.S_ISBLK(os.stat(src).st_mode) else self.block_size(src)
        dsize = os.path.getsize(dst) if not stat.S_ISBLK(os.stat(dst).st_mode) else self.block_size(dst)
        if dsize < ssize:
            raise StepError('The target disk is smaller than the source.')
        self.copy_bytes(src, 0, dst, 0, ssize, 'Cloning disk')
        t = subprocess.run(['sfdisk', '-J', dst], capture_output=True, text=True)
        label = json.loads(t.stdout)['partitiontable']['label'] if t.returncode == 0 and t.stdout.strip() else None
        if label == 'gpt':
            if dsize > ssize:
                self.run(['sgdisk', '-e', dst])
            self.run(['sgdisk', '-G', dst])          # new disk and partition GUIDs
        elif label == 'dos':
            self.run(['sfdisk', '--disk-id', dst, '0x%08x' % random.getrandbits(32)])
        self.reread(dst)

    def op_convert(self, o):
        disk = o['disk']
        self.need_unmounted_disk(disk)
        if o['table'] == 'gpt':
            self.run(['sgdisk', '--mbrtogpt', disk])
        else:
            nums = ':'.join(str(int(re.search(r'(\d+)$', p['node']).group(1)))
                            for p in self.table(disk).get('partitions', []))
            self.run(['sgdisk', '--gpttombr=' + nums, disk] if nums else ['sgdisk', '--zap-all', disk])
        self.reread(disk)


def apply_ops(ops, dry_run=False):
    ex = Executor(dry_run)
    total = len(ops)
    for i, o in enumerate(ops, 1):
        event(event='step', index=i, total=total, kind=o['kind'], desc=o.get('desc', o['kind']))
        try:
            ex.do(o)
        except StepError as e:
            event(event='error', message=str(e), index=i)
            return 1
        except Exception as e:  # never leave the user without an answer
            event(event='error', message=f'Unexpected error: {e}', index=i)
            return 1
    event(event='done')
    return 0


def surface_test(disk):
    """Read the whole disk looking for unreadable blocks."""
    ex = Executor()
    ex.check_disk(disk)
    size = ex.block_size(disk) if stat.S_ISBLK(os.stat(disk).st_mode) else os.path.getsize(disk)
    bad = 0
    done, last = 0, -1
    block = MiB
    with open(disk, 'rb', buffering=0) as f:
        while done < size:
            n = min(block, size - done)
            f.seek(done)
            try:
                f.read(n)
            except OSError:
                bad += 1
                event(event='bad', offset=done)
            done += n
            pct = int(done * 100 / size)
            if pct != last:
                last = pct
                event(event='progress', label='Reading disk', percent=pct, bad=bad)
    event(event='done', bad=bad)
    return 0


def smart(disk):
    if not shutil.which('smartctl'):
        event(event='error', message='smartmontools is not installed. Install it with: sudo apt install smartmontools')
        return 1
    p = subprocess.run(['smartctl', '-H', '-A', '-i', '-j', disk], capture_output=True, text=True)
    try:
        data = json.loads(p.stdout)
    except ValueError:
        event(event='error', message='smartctl returned nothing readable.')
        return 1
    event(event='smart', data=data)
    event(event='done')
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print('usage: paddock-helper apply|surface DISK|smart DISK', file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == 'apply':
        payload = json.load(sys.stdin)
        return apply_ops(payload['ops'], payload.get('dry_run', False))
    if cmd == 'surface' and len(argv) == 2:
        return surface_test(argv[1])
    if cmd == 'smart' and len(argv) == 2:
        return smart(argv[1])
    print('bad arguments', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
