"""Runs every operation for real on disk image files, then compares what the disk holds
with what the in-memory simulation predicted. Needs no root and never touches a real disk."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from slicepm import helper, ops, scan
from slicepm.model import MiB, OpError


def run_ops(oplist, dry=False):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = helper.apply_ops(oplist, dry)
    events = [json.loads(l) for l in buf.getvalue().splitlines()]
    errors = [e for e in events if e['event'] == 'error']
    if rc != 0:
        raise AssertionError(errors[0]['message'] if errors else 'helper failed')
    return events


def blank(path, mib):
    with open(path, 'wb') as f:
        f.truncate(mib * MiB)
    return path


def region(path, start, size, tmp):
    out = os.path.join(tmp, 'region')
    with open(path, 'rb') as f, open(out, 'wb') as o:
        f.seek(start)
        o.write(f.read(size))
    return out


class SliceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.img = blank(os.path.join(self.dir.name, 'a.img'), 512)

    def tearDown(self):
        self.dir.cleanup()

    # -- helpers
    def disks(self, *paths):
        return {d.path: d for d in (scan.scan_image(p) for p in (paths or (self.img,)))}

    def apply(self, *oplist, check_sim=True):
        before = self.disks()
        predicted = ops.replay(before, list(oplist))
        run_ops([dict(o, desc=ops.describe(o, before)) for o in oplist])
        after = self.disks()
        if check_sim:
            self.assert_same(predicted[self.img], after[self.img])
        return after[self.img]

    def assert_same(self, want, got):
        self.assertEqual(want.table, got.table)
        w = [(p.start, p.size, p.fs) for p in want.sorted_parts()]
        g = [(p.start, p.size, p.fs) for p in got.sorted_parts()]
        self.assertEqual(w, g)

    def table(self, kind='gpt'):
        return ops.op('create_table', disk=self.img, table=kind)

    def create(self, start_mib, size_mib, fs=None, label='', flags=None):
        return ops.op('create', disk=self.img, start=start_mib * MiB, size=size_mib * MiB,
                      fs=fs, label=label, flags=flags or [])

    def debugfs(self, p, cmd):
        r = region(self.img, p.start, p.size, self.dir.name)
        out = subprocess.run(['debugfs', '-R', cmd, r], capture_output=True)
        return out.stdout

    # -- tests
    def test_create_and_formats(self):
        d = self.apply(self.table(), self.create(1, 100, 'ext4', 'DATA'),
                       self.create(101, 64, 'vfat', 'boot', ['esp']),
                       self.create(165, 100, 'ntfs', 'WIN'), self.create(265, 50, 'exfat', 'X'),
                       self.create(315, 40, 'swap'))
        got = {p.fs: p for p in d.parts}
        self.assertEqual(got['ext4'].label, 'DATA')
        self.assertEqual(got['vfat'].label.upper(), 'BOOT')
        self.assertIn('esp', got['vfat'].flags)
        self.assertEqual(got['ntfs'].label, 'WIN')
        self.assertEqual(set(got), {'ext4', 'vfat', 'ntfs', 'exfat', 'swap'})

    def test_delete_and_free_space(self):
        d = self.apply(self.table(), self.create(1, 100, 'ext4'), self.create(101, 100, 'ext4'))
        first = d.sorted_parts()[0]
        d = self.apply(ops.op('delete', disk=self.img, start=first.start))
        self.assertEqual(len(d.parts), 1)
        self.assertEqual(d.free_regions()[0][0], MiB)

    def test_resize_grow_and_shrink_keeps_files(self):
        d = self.apply(self.table(), self.create(1, 100, 'ext4', 'KEEP'))
        p = d.parts[0]
        with open(os.path.join(self.dir.name, 'f.txt'), 'w') as f:
            f.write('precious data\n')
        r = region(self.img, p.start, p.size, self.dir.name)
        subprocess.run(['debugfs', '-w', '-R', f'write {self.dir.name}/f.txt f.txt', r], capture_output=True, check=True)
        with open(self.img, 'r+b') as f, open(r, 'rb') as rf:
            f.seek(p.start)
            f.write(rf.read())
        grow = ops.op('resize', disk=self.img, start=p.start, size=p.size, new_start=p.start, new_size=300 * MiB)
        d = self.apply(grow)
        p = d.parts[0]
        self.assertEqual(p.size, 300 * MiB)
        self.assertEqual(self.debugfs(p, 'cat f.txt'), b'precious data\n')
        shrink = ops.op('resize', disk=self.img, start=p.start, size=p.size, new_start=p.start, new_size=40 * MiB)
        d = self.apply(shrink)
        p = d.parts[0]
        self.assertEqual(p.size, 40 * MiB)
        self.assertEqual(self.debugfs(p, 'cat f.txt'), b'precious data\n')
        self.assertEqual(p.label, 'KEEP')

    def test_move_keeps_files(self):
        d = self.apply(self.table(), self.create(1, 100, 'ext4'))
        p = d.parts[0]
        with open(os.path.join(self.dir.name, 'm.txt'), 'w') as f:
            f.write('moved\n')
        r = region(self.img, p.start, p.size, self.dir.name)
        subprocess.run(['debugfs', '-w', '-R', f'write {self.dir.name}/m.txt m.txt', r], capture_output=True, check=True)
        with open(self.img, 'r+b') as f, open(r, 'rb') as rf:
            f.seek(p.start)
            f.write(rf.read())
        move = ops.op('resize', disk=self.img, start=p.start, size=p.size, new_start=200 * MiB, new_size=p.size)
        d = self.apply(move)
        self.assertEqual(d.parts[0].start, 200 * MiB)
        self.assertEqual(self.debugfs(d.parts[0], 'cat m.txt'), b'moved\n')
        # and back, overlapping the old position
        back = ops.op('resize', disk=self.img, start=200 * MiB, size=p.size, new_start=50 * MiB, new_size=p.size)
        d = self.apply(back)
        self.assertEqual(self.debugfs(d.parts[0], 'cat m.txt'), b'moved\n')

    def test_ntfs_shrink_and_grow(self):
        d = self.apply(self.table(), self.create(1, 120, 'ntfs', 'N'))
        p = d.parts[0]
        d = self.apply(ops.op('resize', disk=self.img, start=p.start, size=p.size, new_start=p.start, new_size=60 * MiB))
        d = self.apply(ops.op('resize', disk=self.img, start=p.start, size=60 * MiB, new_start=p.start, new_size=200 * MiB))
        self.assertEqual(d.parts[0].fs, 'ntfs')

    def test_label_flags_format_check(self):
        d = self.apply(self.table(), self.create(1, 100, 'ext4', 'OLD'), self.create(101, 64, 'vfat'))
        a, b = d.sorted_parts()
        d = self.apply(ops.op('label', disk=self.img, start=a.start, label='NEW'))
        self.assertEqual(d.sorted_parts()[0].label, 'NEW')
        d = self.apply(ops.op('flags', disk=self.img, start=b.start, flags=['esp']))
        self.assertIn('esp', d.sorted_parts()[1].flags)
        d = self.apply(ops.op('format', disk=self.img, start=a.start, fs='ntfs', label='Z'))
        self.assertEqual(d.sorted_parts()[0].fs, 'ntfs')
        self.apply(ops.op('check', disk=self.img, start=b.start))

    def test_mbr_limit_and_boot_flag(self):
        self.apply(self.table('msdos'), *[self.create(1 + i * 50, 40, 'ext4') for i in range(4)])
        before = self.disks()
        with self.assertRaises(OpError):
            ops.simulate(before, self.create(250, 40, 'ext4'))
        d = before[self.img]
        d = self.apply(ops.op('flags', disk=self.img, start=d.sorted_parts()[0].start, flags=['boot']))
        self.assertIn('boot', d.sorted_parts()[0].flags)

    def test_copy_partition_to_other_disk(self):
        other = blank(os.path.join(self.dir.name, 'b.img'), 512)
        self.apply(self.table(), self.create(1, 100, 'ext4', 'SRC'))
        run_ops([ops.op('create_table', disk=other, table='gpt')])
        disks = self.disks(self.img, other)
        src = disks[self.img].parts[0]
        c = ops.op('copy', disk=other, src_disk=self.img, src_start=src.start, start=MiB, size=150 * MiB)
        predicted = ops.replay(disks, [c])
        run_ops([c])
        got = scan.scan_image(other)
        self.assert_same(predicted[other], got)
        self.assertEqual(got.parts[0].label, 'SRC')
        self.assertEqual(got.parts[0].size, 150 * MiB)

    def test_clone_wipe_convert(self):
        self.apply(self.table(), self.create(1, 100, 'ext4', 'ONE'), self.create(101, 50, 'vfat'))
        other = blank(os.path.join(self.dir.name, 'c.img'), 600)
        disks = self.disks(self.img, other)
        clone = ops.op('clone', disk=other, src=self.img)
        predicted = ops.replay(disks, [clone])
        run_ops([clone])
        got = scan.scan_image(other)
        self.assert_same(predicted[other], got)
        self.assertEqual([p.label for p in got.sorted_parts()][0], 'ONE')
        run_ops([ops.op('convert', disk=other, table='msdos')])
        self.assertEqual(scan.scan_image(other).table, 'msdos')
        run_ops([ops.op('wipe', disk=other, mode='quick')])
        self.assertIsNone(scan.scan_image(other).table)
        run_ops([ops.op('wipe', disk=self.img, mode='zero')])
        self.assertIsNone(scan.scan_image(self.img).table)

    # -- validation (no disk needed)
    def test_validation(self):
        d = self.apply(self.table(), self.create(1, 100, 'ext4'))
        disks = self.disks()
        with self.assertRaises(OpError):
            ops.simulate(disks, self.create(50, 100, 'ext4'))            # overlaps
        with self.assertRaises(OpError):
            ops.simulate(disks, ops.op('create', disk=self.img, start=500 * MiB, size=100 * MiB, fs='ext4', label=''))
        with self.assertRaises(OpError):
            ops.simulate(disks, ops.op('create', disk=self.img, start=MiB * 101, size=10 * MiB, fs='vfat', label=''))
        disks[self.img].parts[0].mountpoint = '/mnt/x'
        with self.assertRaises(OpError):
            ops.simulate(disks, ops.op('delete', disk=self.img, start=MiB))
        with self.assertRaises(OpError):
            ops.simulate(disks, ops.op('format', disk=self.img, start=MiB, fs='ext4', label=''))
        with self.assertRaises(OpError):
            ops.simulate(disks, ops.op('wipe', disk=self.img, mode='quick'))

    def test_helper_refuses_files_as_root(self):
        ex = helper.Executor()
        ex.is_root = True
        with self.assertRaises(helper.StepError):
            ex.check_disk(self.img)

    def test_failure_stops_and_reports(self):
        self.apply(self.table(), self.create(1, 100, 'ext4'))
        bad = ops.op('delete', disk=self.img, start=999 * MiB)
        with self.assertRaises(AssertionError):
            run_ops([bad])


if __name__ == '__main__':
    unittest.main(verbosity=2)
