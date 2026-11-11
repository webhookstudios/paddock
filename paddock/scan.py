"""Read disks and partitions into the model. Read-only, and needs no root."""
import json
import os
import re
import subprocess

from . import fs as fsinfo
from .model import (Disk, Partition, MBR_EXTENDED, GPT_ESP)


def _run(argv):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out


def _flags(table, ptype, partflags):
    flags = set()
    pt = (ptype or '').lower()
    if pt in (GPT_ESP, '0xef'):
        flags.add('esp')
    if table == 'msdos' and partflags and partflags.lower() in ('0x80',):
        flags.add('boot')
    return flags


def _unescape(text):
    """blkid's export format writes spaces as '\\ ' and other bytes as '\\xNN'."""
    text = text.replace('\\ ', ' ')
    return re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), text)


def scan_devices():
    """All real disks (not loop, ram or optical), with their partitions."""
    out = _run(['lsblk', '-b', '-J', '-e', '1,7,11', '-o',
                'NAME,PATH,TYPE,SIZE,MODEL,VENDOR,TRAN,ROTA,RM,RO,PTTYPE,LOG-SEC,START,PARTN,'
                'FSTYPE,LABEL,UUID,PARTTYPE,PARTFLAGS,MOUNTPOINTS,FSUSED'])
    if not out or out.returncode not in (0, 32):
        raise RuntimeError('Could not list disks: ' + (out.stderr if out else 'lsblk is missing'))
    disks = []
    for dev in json.loads(out.stdout)['blockdevices']:
        if dev['type'] != 'disk':
            continue
        table = {'gpt': 'gpt', 'dos': 'msdos'}.get(dev.get('pttype'))
        d = Disk(path=dev['path'], size=int(dev['size']),
                 name=' '.join(x for x in ((dev.get('model') or '').strip(),) if x),
                 table=table, sector=int(dev.get('log-sec') or 512),
                 removable=bool(dev.get('rm')), transport=dev.get('tran') or '',
                 rotational=dev.get('rota'), readonly=bool(dev.get('ro')))
        if not d.name:
            d.name = (dev.get('vendor') or '').strip()
        for c in dev.get('children') or []:
            if c['type'] != 'part':
                continue
            mounts = [m for m in (c.get('mountpoints') or []) if m]
            fs = fsinfo.normalize(c.get('fstype'))
            ptype = c.get('parttype') or ''
            role = 'primary'
            if table == 'msdos' and ptype.lower().lstrip('0x') in {x.lstrip('0x') for x in MBR_EXTENDED}:
                role = 'extended'
            # Logical partitions on MBR have numbers of 5 and up.
            if table == 'msdos' and (c.get('partn') or 0) >= 5:
                role = 'logical'
            d.parts.append(Partition(
                start=int(c['start'] or 0) * 512, size=int(c['size']), fs=fs,
                label=c.get('label') or '', ptype=ptype, number=c.get('partn'), node=c['path'],
                uuid=c.get('uuid') or '', mountpoint=', '.join(mounts) or None,
                used=int(c['fsused']) if c.get('fsused') else None,
                flags=_flags(table, ptype, c.get('partflags')), role=role))
        disks.append(d)
    return disks


def scan_image(path):
    """Open a disk image file as a disk. Used for demos and for testing."""
    path = os.path.abspath(path)
    size = os.path.getsize(path)
    d = Disk(path=path, size=size, name='Disk image', is_image=True)
    out = _run(['sfdisk', '-J', path])
    if not out or out.returncode != 0 or not out.stdout.strip():
        return d
    table = json.loads(out.stdout)['partitiontable']
    d.table = {'gpt': 'gpt', 'dos': 'msdos'}.get(table['label'])
    d.sector = int(table.get('sectorsize', 512))
    for p in table.get('partitions', []):
        start, psize = p['start'] * d.sector, p['size'] * d.sector
        num = int(''.join(ch for ch in p['node'].rsplit('/', 1)[-1][len(os.path.basename(path)):] if ch.isdigit()) or 0)
        ptype = p['type'].lower()
        if d.table == 'msdos':
            ptype = '0x' + ptype.lstrip('0x').rjust(2, '0')
        fs, label, uuid = None, '', ''
        probe = _run(['blkid', '-p', '-O', str(start), '-S', str(psize), '-o', 'export', path])
        if probe and probe.returncode == 0:
            info = dict(line.split('=', 1) for line in probe.stdout.splitlines() if '=' in line)
            fs, label, uuid = fsinfo.normalize(info.get('TYPE')), _unescape(info.get('LABEL', '')), info.get('UUID', '')
        role = 'primary'
        if d.table == 'msdos' and ptype.lstrip('0x') in {x.lstrip('0x') for x in MBR_EXTENDED}:
            role = 'extended'
        flags = _flags(d.table, ptype, '0x80' if p.get('bootable') else '')
        d.parts.append(Partition(start=start, size=psize, fs=fs, label=label, ptype=ptype,
                                 number=num, node=None, uuid=uuid, flags=flags, role=role))
    return d
