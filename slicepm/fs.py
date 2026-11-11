"""What each filesystem needs: which tools make, label, check and resize it."""
import shutil

MiB = 1 << 20

# name -> description of what the installed tools can do
FILESYSTEMS = {
    'ext4':  dict(title='ext4',  mkfs=['mkfs.ext4', '-F', '-q'], label=16, mkfs_label='-L',
                  check=[['e2fsck', '-f', '-y']], resize='ext', labeler=['tune2fs', '-L']),
    'ext3':  dict(title='ext3',  mkfs=['mkfs.ext3', '-F', '-q'], label=16, mkfs_label='-L',
                  check=[['e2fsck', '-f', '-y']], resize='ext', labeler=['tune2fs', '-L']),
    'ext2':  dict(title='ext2',  mkfs=['mkfs.ext2', '-F', '-q'], label=16, mkfs_label='-L',
                  check=[['e2fsck', '-f', '-y']], resize='ext', labeler=['tune2fs', '-L']),
    'ntfs':  dict(title='NTFS',  mkfs=['mkfs.ntfs', '-F', '-f', '-q'], label=128, mkfs_label='-L',
                  check=[['ntfsfix', '-d']], resize='ntfs', labeler=['ntfslabel']),
    'vfat':  dict(title='FAT32', mkfs=['mkfs.vfat', '-F', '32'], label=11, mkfs_label='-n',
                  check=[['fsck.vfat', '-a']], resize=None, labeler=['fatlabel'], upper=True),
    'exfat': dict(title='exFAT', mkfs=['mkfs.exfat'], label=15, mkfs_label='-L',
                  check=[['fsck.exfat', '-p']], resize=None, labeler=['exfatlabel']),
    'btrfs': dict(title='Btrfs', mkfs=['mkfs.btrfs', '-f', '-q'], label=255, mkfs_label='-L',
                  check=[['btrfs', 'check', '--readonly']], resize=None, labeler=None),
    'xfs':   dict(title='XFS',   mkfs=['mkfs.xfs', '-f', '-q'], label=12, mkfs_label='-L',
                  check=[['xfs_repair', '-n']], resize=None, labeler=['xfs_admin', '-L']),
    'swap':  dict(title='Linux swap', mkfs=['mkswap'], label=15, mkfs_label='-L',
                  check=[], resize='recreate', labeler=['swaplabel', '-L']),
}

# libblkid names that differ from ours
ALIASES = {'ntfs3': 'ntfs', 'fat': 'vfat', 'fat32': 'vfat', 'fat16': 'vfat', 'msdos': 'vfat',
           'linux-swap': 'swap', 'swap': 'swap'}


def normalize(name):
    if not name:
        return None
    name = name.lower()
    return ALIASES.get(name, name)


def available(fs):
    info = FILESYSTEMS.get(fs)
    return bool(info) and shutil.which(info['mkfs'][0]) is not None


def creatable():
    """Filesystems that can be created here, in display order."""
    order = ['ext4', 'ntfs', 'vfat', 'exfat', 'btrfs', 'xfs', 'ext3', 'ext2', 'swap']
    return [f for f in order if available(f)]


def clean_label(fs, label):
    info = FILESYSTEMS.get(fs)
    label = (label or '').strip()
    if not info:
        return label
    if info.get('upper'):
        label = label.upper()
    return label[:info['label']]


def can_resize(fs):
    """True when Slice can resize the filesystem itself, not just the partition."""
    if fs is None:
        return True
    info = FILESYSTEMS.get(fs)
    if not info or not info['resize']:
        return False
    if info['resize'] == 'ext':
        return shutil.which('resize2fs') is not None
    if info['resize'] == 'ntfs':
        return shutil.which('ntfsresize') is not None
    return True


def resize_reason(fs):
    if fs is None:
        return None
    info = FILESYSTEMS.get(fs)
    if info and info['resize'] and can_resize(fs):
        return None
    if fs in ('vfat', 'exfat'):
        return f'Resizing {info["title"] if info else fs} needs a tool Ubuntu does not ship (fatresize or an exFAT resizer).'
    if fs in ('btrfs', 'xfs'):
        return f'{info["title"]} can only be resized while mounted, which Paddock does not do.'
    return f'Paddock cannot resize {fs} filesystems.'


def can_check(fs):
    info = FILESYSTEMS.get(fs or '')
    if not info or not info['check']:
        return False
    return shutil.which(info['check'][0][0]) is not None


def can_label(fs):
    info = FILESYSTEMS.get(fs or '')
    return bool(info and info['labeler'] and shutil.which(info['labeler'][0]))
