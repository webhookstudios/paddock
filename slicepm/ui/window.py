import json
import os

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, GObject, Gtk

from .. import __version__
from .. import fs as fsinfo
from .. import ops
from ..model import GiB, MiB, OpError, human
from . import dialogs
from .diskmap import DiskMap, color_for
from .runner import HelperProcess
from .state import State

COLUMNS = [('Partition', 150), ('Filesystem', 110), ('Label', 170), ('Size', 90), ('Used', 90),
           ('Free', 90), ('Flags', 80)]

CSS = b"""
.col-head { font-weight: 700; font-size: 0.85em; opacity: 0.65; }
.dim { opacity: 0.6; }
.badge-new { color: @accent_color; font-weight: 700; font-size: 0.8em; }
.pending-bar { background: alpha(@accent_bg_color, 0.14); border-radius: 12px; padding: 6px 8px 6px 14px; }
.action-bar button { padding: 6px 12px; }
.swatch { min-width: 10px; min-height: 10px; border-radius: 3px; }
.partrow { padding: 8px 12px; }
"""


def swatch(fs):
    box = Gtk.Box()
    box.add_css_class('swatch')
    r, g, b = color_for(fs)
    prov = Gtk.CssProvider()
    prov.load_from_string('.swatch { background: rgb(%d,%d,%d); }' % (r * 255, g * 255, b * 255))
    box.get_style_context().add_provider(prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    box.set_valign(Gtk.Align.CENTER)
    return box


class SliceWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='Slice', default_width=1180, default_height=760)
        self.state = State()
        self.sel_disk = None
        self.sel_item = None            # ('p'|'f', start)
        self.buttons = {}
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._build()
        self._actions()
        self.rescan()

    # ---------------------------------------------------------------- layout

    def _build(self):
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.apply_btn = Gtk.Button(label='Apply', css_classes=['suggested-action'], sensitive=False,
                                    tooltip_text='Write the pending changes to the disks')
        self.apply_btn.connect('clicked', lambda *_: self.on_apply())
        header.pack_start(self.apply_btn)
        refresh = Gtk.Button(icon_name='view-refresh-symbolic', tooltip_text='Rescan disks')
        refresh.connect('clicked', lambda *_: self.rescan())
        header.pack_start(refresh)
        menu = Gio.Menu()
        menu.append('Open disk image…', 'win.open_image')
        menu.append('New practice disk image…', 'win.new_image')
        sect = Gio.Menu()
        sect.append('About Slice', 'win.about')
        menu.append_section(None, sect)
        header.pack_end(Gtk.MenuButton(icon_name='open-menu-symbolic', menu_model=menu, primary=True))
        view.add_top_bar(header)

        split = Adw.OverlaySplitView(sidebar_width_fraction=0.25, min_sidebar_width=250, max_sidebar_width=330)
        # sidebar
        self.disk_list = Gtk.ListBox(css_classes=['navigation-sidebar'], selection_mode=Gtk.SelectionMode.SINGLE)
        self.disk_list.connect('row-selected', self._disk_row_selected)
        side = Adw.ToolbarView()
        side.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False,
                                       title_widget=Gtk.Label(label='Disks', css_classes=['heading'])))
        side.set_content(Gtk.ScrolledWindow(child=self.disk_list, hscrollbar_policy=Gtk.PolicyType.NEVER))
        split.set_sidebar(side)

        # content
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=14, margin_bottom=14,
                          margin_start=16, margin_end=16)
        self.disk_title = Gtk.Label(xalign=0, css_classes=['title-3'])
        self.disk_sub = Gtk.Label(xalign=0, css_classes=['dim'])
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        head.append(self.disk_title)
        head.append(self.disk_sub)
        content.append(head)

        self.map = DiskMap()
        self.map.connect('picked', lambda _m, kind, start: self.select(kind, start))
        content.append(self.map)

        self.legend = Gtk.Box(spacing=14)
        content.append(self.legend)

        self.action_bar = self._action_bar()
        content.append(self.action_bar)

        colhead = Gtk.Box(spacing=0, margin_start=12, margin_end=12)
        colhead.append(Gtk.Box(width_request=26))
        for title, width in COLUMNS:
            colhead.append(Gtk.Label(label=title, xalign=0, width_request=width, css_classes=['col-head']))
        colhead.append(Gtk.Label(label='Mounted at', xalign=0, hexpand=True, css_classes=['col-head']))
        content.append(colhead)

        self.table = Gtk.ListBox(css_classes=['boxed-list'], selection_mode=Gtk.SelectionMode.SINGLE)
        self.table.connect('row-selected', self._row_selected)
        self.empty = Adw.StatusPage(icon_name='drive-harddisk-symbolic', title='No disk selected',
                                    description='Pick a disk on the left.', vexpand=True, visible=False)
        self.table_scroll = Gtk.ScrolledWindow(child=self.table, vexpand=True,
                                               hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        content.append(self.table_scroll)
        content.append(self.empty)

        self.pending_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        bar = Gtk.Box(spacing=8, css_classes=['pending-bar'])
        self.pending_label = Gtk.Label(xalign=0, hexpand=True)
        bar.append(self.pending_label)
        for text, cb in (('Review', self.on_review), ('Undo last', self.on_undo), ('Discard all', self.on_discard)):
            b = Gtk.Button(label=text, css_classes=['flat'])
            b.connect('clicked', lambda _b, cb=cb: cb())
            bar.append(b)
        self.pending_rev.set_child(bar)
        content.append(self.pending_rev)

        split.set_content(content)
        view.set_content(split)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

    def _action_bar(self):
        bar = Gtk.Box(spacing=4, css_classes=['action-bar'])
        specs = [
            ('create', 'list-add-symbolic', 'New', self.a_create),
            ('delete', 'user-trash-symbolic', 'Delete', self.a_delete),
            ('format', 'edit-clear-all-symbolic', 'Format', self.a_format),
            ('resize', 'object-flip-horizontal-symbolic', 'Resize / Move', self.a_resize),
            ('label', 'tag-symbolic', 'Label', self.a_label),
            ('flags', 'emblem-system-symbolic', 'Flags', self.a_flags),
            ('check', 'emblem-ok-symbolic', 'Check', self.a_check),
            ('copy', 'edit-copy-symbolic', 'Copy', self.a_copy),
            ('mount', 'media-eject-symbolic', 'Mount', self.a_mount),
        ]
        for key, icon, text, cb in specs:
            content = Adw.ButtonContent(icon_name=icon, label=text)
            b = Gtk.Button(child=content, css_classes=['flat'])
            b.connect('clicked', lambda _b, cb=cb: cb())
            self.buttons[key] = (b, content)
            bar.append(b)
        bar.append(Gtk.Box(hexpand=True))
        disk_menu = Gio.Menu()
        disk_menu.append('New partition table…', 'win.new_table')
        disk_menu.append('Convert table type…', 'win.convert_table')
        disk_menu.append('Clone another disk onto this one…', 'win.clone')
        disk_menu.append('Wipe disk…', 'win.wipe')
        health = Gio.Menu()
        health.append('Disk health (SMART)', 'win.smart')
        health.append('Surface test (read check)', 'win.surface')
        disk_menu.append_section(None, health)
        self.disk_menu_btn = Gtk.MenuButton(menu_model=disk_menu, css_classes=['flat'],
                                            child=Adw.ButtonContent(icon_name='drive-harddisk-symbolic',
                                                                    label='Disk'))
        bar.append(self.disk_menu_btn)
        return bar

    def _actions(self):
        def add(name, cb):
            a = Gio.SimpleAction.new(name, None)
            a.connect('activate', lambda *_: cb())
            self.add_action(a)
            return a
        add('open_image', self.a_open_image)
        add('new_image', self.a_new_image)
        add('about', self.a_about)
        self.disk_actions = {
            'new_table': add('new_table', lambda: self._with_disk(dialogs.new_table)),
            'convert_table': add('convert_table', lambda: self._with_disk(dialogs.convert_table)),
            'wipe': add('wipe', lambda: self._with_disk(dialogs.wipe_disk)),
            'clone': add('clone', self.a_clone),
            'smart': add('smart', self.a_smart),
            'surface': add('surface', self.a_surface),
        }

    # ----------------------------------------------------------- state / refresh

    def rescan(self):
        self.state.rescan()
        if self.state.error:
            self.toast(self.state.error)
        self.refresh()

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=5))

    def queue(self, o):
        try:
            self.state.queue(o)
        except OpError as e:
            self.toast(str(e))
            return
        if o['kind'] in ('create', 'copy') or o['kind'] == 'resize':
            self.sel_item = None
        self.refresh()

    def disk(self):
        return self.state.disks.get(self.sel_disk)

    def part(self):
        d = self.disk()
        if d and self.sel_item and self.sel_item[0] == 'p':
            for p in d.parts:
                if p.start == self.sel_item[1]:
                    return p
        return None

    def region(self):
        d = self.disk()
        if d and self.sel_item and self.sel_item[0] == 'f':
            for s, z in d.free_regions():
                if s == self.sel_item[1]:
                    return s, z
        return None

    def refresh(self):
        disks = list(self.state.disks.values())
        if self.sel_disk not in self.state.disks:
            self.sel_disk = disks[0].path if disks else None
            self.sel_item = None
        # sidebar
        self._rebuilding = True
        while (row := self.disk_list.get_first_child()):
            self.disk_list.remove(row)
        touched = self.state.touched()
        select_row = None
        for d in disks:
            icon = ('application-x-cd-image-symbolic' if d.is_image else
                    'media-removable-symbolic' if d.removable else 'drive-harddisk-symbolic')
            sub = ' · '.join(x for x in (human(d.size), {'gpt': 'GPT', 'msdos': 'MBR'}.get(d.table, 'No table'),
                                          d.transport.upper() if d.transport else '') if x)
            row = Adw.ActionRow(title=GLib.markup_escape_text(d.title()), subtitle=sub, activatable=True)
            row.add_prefix(Gtk.Image(icon_name=icon))
            if d.path in touched:
                row.add_suffix(Gtk.Label(label='●', css_classes=['badge-new'], tooltip_text='Has pending changes'))
            row.disk_path = d.path
            self.disk_list.append(row)
            if d.path == self.sel_disk:
                select_row = row
        if select_row:
            self.disk_list.select_row(select_row)
        self._rebuilding = False
        self._render_disk()
        n = len(self.state.pending)
        self.pending_rev.set_reveal_child(n > 0)
        self.pending_label.set_label(f'{n} pending change{"s" if n != 1 else ""}. Nothing has been written yet.')
        self.apply_btn.set_sensitive(n > 0)
        self.apply_btn.set_label(f'Apply ({n})' if n else 'Apply')

    def _render_disk(self):
        d = self.disk()
        self.empty.set_visible(d is None)
        self.table_scroll.set_visible(d is not None)
        if not d:
            self.disk_title.set_label('')
            self.disk_sub.set_label('')
            self.map.set_disk(None)
            self._sensitivity()
            return
        self.disk_title.set_label(d.title())
        bits = [human(d.size), {'gpt': 'GPT partition table', 'msdos': 'MBR partition table'}.get(d.table, 'No partition table')]
        if d.readonly:
            bits.append('read-only')
        if d.is_image:
            bits.append(os.path.basename(d.path))
            self.disk_sub.set_tooltip_text(d.path)
        else:
            self.disk_sub.set_tooltip_text(None)
        self.disk_sub.set_label(' · '.join(bits))
        self.map.set_disk(d, self.sel_item)

        used = []
        for p in d.parts:
            if p.fs not in used:
                used.append(p.fs)
        while (c := self.legend.get_first_child()):
            self.legend.remove(c)
        for fs in used:
            item = Gtk.Box(spacing=6)
            item.append(swatch(fs))
            item.append(Gtk.Label(label=fsinfo.FILESYSTEMS.get(fs, {}).get('title', fs or 'Unformatted'),
                                  css_classes=['dim']))
            self.legend.append(item)

        self._loading = True
        while (row := self.table.get_first_child()):
            self.table.remove(row)
        entries = [('p', p.start, p) for p in d.parts if True]
        entries += [('f', s, (s, z)) for s, z in d.free_regions()]
        entries.sort(key=lambda e: e[1])
        sel_row = None
        for kind, start, obj in entries:
            row = self._row(d, kind, obj)
            row.key = (kind, start)
            self.table.append(row)
            if self.sel_item == (kind, start):
                sel_row = row
        if sel_row:
            self.table.select_row(sel_row)
        self._loading = False
        if not entries:
            self.table.append(Adw.ActionRow(title='This disk is empty', subtitle='Create a partition table to begin.'))
        self._sensitivity()

    def _row(self, d, kind, obj):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(css_classes=['partrow'])
        if kind == 'f':
            start, size = obj
            box.append(Gtk.Box(width_request=26))
            box.append(Gtk.Label(label='Unallocated', xalign=0, width_request=COLUMNS[0][1], css_classes=['dim']))
            for _ in range(2):
                box.append(Gtk.Label(label='', width_request=COLUMNS[1][1] if _ == 0 else COLUMNS[2][1]))
            box.append(Gtk.Label(label=human(size), xalign=0, width_request=COLUMNS[3][1]))
            row.set_child(box)
            return row
        p = obj
        box.append(swatch(p.fs))
        box.get_first_child().set_margin_end(16)
        name = p.title()
        lab = Gtk.Label(label=name, xalign=0, width_request=COLUMNS[0][1] - 0)
        if p.new:
            lab.add_css_class('badge-new')
        box.append(lab)
        fs_title = fsinfo.FILESYSTEMS.get(p.fs, {}).get('title', p.fs or 'Unformatted')
        flags = ', '.join({'esp': 'EFI', 'boot': 'Boot'}.get(f, f) for f in sorted(p.flags))
        if p.role != 'primary':
            fs_title = 'Extended' if p.role == 'extended' else (fs_title + ' (logical)')
        free = human(p.size - p.used) if p.used is not None else '—'
        cells = [fs_title, p.label or '—', human(p.size), human(p.used) if p.used is not None else '—', free, flags]
        for text, (_, width) in zip(cells, COLUMNS[1:]):
            box.append(Gtk.Label(label=text, xalign=0, width_request=width, ellipsize=3, max_width_chars=22))
        box.append(Gtk.Label(label=p.mountpoint or '', xalign=0, hexpand=True, ellipsize=3))
        row.set_child(box)
        return row

    def _sensitivity(self):
        d, p, r = self.disk(), self.part(), self.region()
        editable = bool(d and p and p.editable and not d.readonly)
        ro = bool(d and d.readonly)
        self.buttons['create'][0].set_sensitive(bool(r) and not ro)
        for key in ('delete', 'format', 'resize', 'label', 'flags', 'check', 'copy'):
            self.buttons[key][0].set_sensitive(editable)
        mount_ok = bool(d and p and p.node and p.fs and not p.new and not d.is_image and p.role == 'primary')
        self.buttons['mount'][0].set_sensitive(mount_ok)
        content = self.buttons['mount'][1]
        content.set_label('Unmount' if (p and p.mounted) else 'Mount')
        for key in ('new_table', 'convert_table', 'wipe', 'clone'):
            self.disk_actions[key].set_enabled(bool(d) and not (d and d.readonly))
        self.disk_actions['convert_table'].set_enabled(bool(d and d.table) and not d.readonly)
        for key in ('smart', 'surface'):
            self.disk_actions[key].set_enabled(bool(d) and not d.is_image or key == 'surface' and bool(d))

    # ------------------------------------------------------------- selection

    def _disk_row_selected(self, lb, row):
        if getattr(self, '_rebuilding', False) or row is None:
            return
        self.sel_disk = row.disk_path
        self.sel_item = None
        self._render_disk()

    def _row_selected(self, lb, row):
        if getattr(self, '_loading', False):
            return
        self.sel_item = getattr(row, 'key', None) if row else None
        self.map.set_disk(self.disk(), self.sel_item)
        self._sensitivity()

    def select(self, kind, start):
        self.sel_item = (kind, start)
        self._render_disk()

    # ---------------------------------------------------------------- actions

    def _with_disk(self, fn):
        d = self.disk()
        if d:
            fn(self, d, self.queue)

    def a_create(self):
        d, r = self.disk(), self.region()
        if d and r:
            dialogs.create_partition(self, d, r[0], r[1], self.queue)

    def a_delete(self):
        d, p = self.disk(), self.part()
        if d and p:
            dialogs.ask(self, 'Delete partition?', f'{p.title()} ({human(p.size)}) will be removed when you apply.',
                        None, 'Add to changes', 'destructive',
                        lambda: self.queue(ops.op('delete', disk=d.path, start=p.start)))

    def a_format(self):
        d, p = self.disk(), self.part()
        if d and p:
            dialogs.format_partition(self, d, p, self.queue)

    def a_resize(self):
        d, p = self.disk(), self.part()
        if d and p:
            dialogs.resize_partition(self, d, p, self.queue)

    def a_label(self):
        d, p = self.disk(), self.part()
        if d and p:
            dialogs.set_label(self, d, p, self.queue)

    def a_flags(self):
        d, p = self.disk(), self.part()
        if d and p:
            dialogs.set_flags(self, d, p, self.queue)

    def a_check(self):
        d, p = self.disk(), self.part()
        if d and p:
            self.queue(ops.op('check', disk=d.path, start=p.start))

    def a_copy(self):
        d, p = self.disk(), self.part()
        if d and p:
            dialogs.copy_partition(self, self.state.disks, d, p, self.queue)

    def a_clone(self):
        d = self.disk()
        if d:
            dialogs.clone_disk(self, self.state.disks, d, self.queue)

    def a_mount(self):
        p = self.part()
        if not p or not p.node:
            return
        verb = 'unmount' if p.mounted else 'mount'
        try:
            proc = Gio.Subprocess.new(['udisksctl', verb, '-b', p.node],
                                      Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as e:
            self.toast(str(e))
            return

        def done(pr, res):
            try:
                ok, out, _ = pr.communicate_utf8_finish(res)
            except GLib.Error as e:
                self.toast(str(e))
                return
            self.toast((out or '').strip().splitlines()[-1] if out and out.strip() else f'{verb} finished')
            self.state.rescan_keep_pending()
            self.refresh()
        proc.communicate_utf8_async(None, None, done)

    # ------------------------------------------------------------ image files

    def a_open_image(self):
        dlg = Gtk.FileDialog(title='Open a disk image')
        dlg.open(self, None, self._image_chosen)

    def _image_chosen(self, dlg, res):
        try:
            f = dlg.open_finish(res)
        except GLib.Error:
            return
        self.state.add_image(f.get_path())
        self.sel_disk = os.path.abspath(f.get_path())
        self.sel_item = None
        self.refresh()

    def a_new_image(self):
        size = dialogs._spin('Size', 'GiB. The file starts almost empty and only grows as you use it.', 8, 1, 256)
        dialogs.ask(self, 'New practice disk image',
                    'A file that acts like a disk, so you can try Slice without touching a real one.',
                    dialogs._group(size), 'Choose where to save…', on_ok=lambda: self._pick_new_image(int(size.get_value())))

    def _pick_new_image(self, gib):
        dlg = Gtk.FileDialog(title='Save the practice disk image', initial_name='practice-disk.img')

        def done(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            path = f.get_path()
            try:
                with open(path, 'wb') as fh:
                    fh.truncate(gib * GiB)
            except OSError as e:
                self.toast(f'Could not create the image: {e}')
                return
            self.state.add_image(path)
            self.sel_disk = os.path.abspath(path)
            self.sel_item = None
            self.refresh()
        dlg.save(self, None, done)

    def a_about(self):
        about = Adw.AboutDialog(application_name='Slice', application_icon='io.github.webhookstudios.Slice',
                                version=__version__, developer_name='webhookstudios',
                                website='https://github.com/webhookstudios/slice',
                                issue_url='https://github.com/webhookstudios/slice/issues',
                                license_type=Gtk.License.GPL_2_0,
                                comments='Create, resize, move, copy, format and check disk partitions.')
        about.present(self)

    # ----------------------------------------------------------- apply changes

    def on_undo(self):
        self.state.undo()
        self.refresh()

    def on_discard(self):
        self.state.discard()
        self.refresh()

    def _ops_list(self):
        box = Gtk.ListBox(css_classes=['boxed-list'], selection_mode=Gtk.SelectionMode.NONE)
        for i, o in enumerate(self.state.pending, 1):
            row = Adw.ActionRow(title=GLib.markup_escape_text(o.get('desc', o['kind'])), title_lines=0)
            row.add_prefix(Gtk.Label(label=str(i), css_classes=['dim']))
            box.append(row)
        return Gtk.ScrolledWindow(child=box, min_content_height=min(300, 56 * len(self.state.pending) + 8),
                                  max_content_height=320, propagate_natural_height=True,
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)

    def on_review(self):
        dlg = Adw.AlertDialog(heading='Pending changes', body='Nothing is written until you press Apply.')
        dlg.set_extra_child(self._ops_list())
        dlg.add_response('close', 'Close')
        dlg.set_close_response('close')
        dlg.choose(self, None, lambda d, r: d.choose_finish(r))

    def on_apply(self):
        if not self.state.pending:
            return
        risky = any(ops.is_destructive(o) for o in self.state.pending)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self._ops_list())
        confirm = Adw.SwitchRow(title='I have a backup of anything important')
        if risky:
            box.append(Gtk.ListBox(css_classes=['boxed-list'], selection_mode=Gtk.SelectionMode.NONE))
            box.get_last_child().append(confirm)
        n = len(self.state.pending)
        dlg = Adw.AlertDialog(
            heading=f'Apply {n} change{"s" if n != 1 else ""}?',
            body=('This changes your disks and cannot be undone. Do not unplug or power off until it finishes.'
                  if risky else 'This will write the changes to your disks.'))
        dlg.set_extra_child(box)
        dlg.add_response('cancel', 'Cancel')
        dlg.add_response('apply', 'Apply')
        dlg.set_response_appearance('apply', Adw.ResponseAppearance.DESTRUCTIVE if risky
                                    else Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response('cancel')
        dlg.set_close_response('cancel')
        if risky:
            dlg.set_response_enabled('apply', False)
            confirm.connect('notify::active', lambda r, _p: dlg.set_response_enabled('apply', r.get_active()))
        dlg.choose(self, None, lambda d, r: d.choose_finish(r) == 'apply' and self._run_apply())

    def _run_apply(self):
        ProgressDialog(self, 'Applying changes', ['apply'], not self.state.uses_images(),
                       payload={'ops': self.state.pending}, on_finished=self._applied).present(self)

    def _applied(self, ok):
        self.state.rescan()
        self.refresh()
        if ok:
            self.toast('All changes were applied.')

    # ----------------------------------------------------------- health checks

    def a_smart(self):
        d = self.disk()
        if not d:
            return
        if d.is_image:
            self.toast('Disk images have no health data.')
            return
        SmartDialog(self, d).present(self)

    def a_surface(self):
        d = self.disk()
        if d:
            ProgressDialog(self, f'Surface test of {d.title()}', ['surface', d.path], not d.is_image,
                           bad_counter=True).present(self)


class ProgressDialog(Adw.Dialog):
    def __init__(self, window, title, args, elevated, payload=None, on_finished=None, bad_counter=False):
        super().__init__(title=title, content_width=560, can_close=False)
        self.on_finished = on_finished
        self.total_steps = 1
        self.step = 1
        self.bad = 0
        self.ok = False
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=16,
                      margin_start=16, margin_end=16)
        self.status = Gtk.Label(label='Waiting for permission…', xalign=0, wrap=True)
        self.bar = Gtk.ProgressBar()
        self.detail = Gtk.Label(label='', xalign=0, css_classes=['dim'])
        box.append(self.status)
        box.append(self.bar)
        box.append(self.detail)
        self.log = Gtk.TextView(editable=False, monospace=True, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                left_margin=8, right_margin=8, top_margin=6, bottom_margin=6)
        scroll = Gtk.ScrolledWindow(child=self.log, min_content_height=170, vexpand=True, css_classes=['card'])
        exp = Gtk.Expander(label='Details', child=scroll)
        box.append(exp)
        self.button = Gtk.Button(label='Cancel', halign=Gtk.Align.END)
        self.button.connect('clicked', self._button)
        box.append(self.button)
        view.set_content(box)
        self.set_child(view)
        self.pulse_id = GLib.timeout_add(120, self._pulse)
        self.has_progress = False
        try:
            self.proc = HelperProcess(args, elevated, self._event, payload)
        except GLib.Error as e:
            self.proc = None
            GLib.idle_add(lambda: self._event({'event': 'error', 'message': e.message}))

    def _pulse(self):
        if not self.has_progress:
            self.bar.pulse()
        return self.pulse_id != 0

    def _append(self, text):
        buf = self.log.get_buffer()
        buf.insert(buf.get_end_iter(), text + '\n')

    def _event(self, ev):
        k = ev.get('event')
        if k == 'step':
            self.step, self.total_steps = ev['index'], ev['total']
            self.status.set_label(f'Step {self.step} of {self.total_steps}: {ev.get("desc", "")}')
            self.has_progress = False
            self._append(f'--- {ev.get("desc", "")}')
        elif k == 'progress':
            self.has_progress = True
            frac = ev['percent'] / 100
            self.bar.set_fraction(((self.step - 1) + frac) / self.total_steps)
            self.detail.set_label(f'{ev.get("label", "")} {ev["percent"]}%' + (f' · {ev["bad"]} unreadable' if 'bad' in ev else ''))
            if 'bad' in ev:
                self.status.set_label('Reading every block of the disk…')
        elif k == 'bad':
            self.bad += 1
            self._append(f'Unreadable block near {human(ev["offset"])}')
        elif k == 'log':
            self._append(ev.get('line', ''))
        elif k == 'done':
            self._finish(True, ev)
        elif k == 'error':
            self._finish(False, ev)

    def _finish(self, ok, ev):
        if self.pulse_id:
            GLib.source_remove(self.pulse_id)
            self.pulse_id = 0
        self.ok = ok
        self.set_can_close(True)
        self.has_progress = True
        if ok:
            self.bar.set_fraction(1.0)
            bad = ev.get('bad')
            if bad is not None:
                self.status.set_label('The disk read cleanly.' if bad == 0 else f'{bad} unreadable blocks were found. Back up and replace this disk.')
            else:
                self.status.set_label('Finished.')
            self.detail.set_label('')
        else:
            self.status.set_label(ev.get('message', 'Something went wrong.'))
            self.status.add_css_class('error')
            self.detail.set_label('Some changes may already have been written. Open Details for the log.')
        self.button.set_label('Close')
        self.button.add_css_class('suggested-action')
        self.button.remove_css_class('destructive-action')
        self.finished = True

    def _button(self, *_):
        if getattr(self, 'finished', False):
            self.close()
            if self.on_finished:
                self.on_finished(self.ok)
        else:
            if self.proc:
                self.proc.cancel()
            self._finish(False, {'message': 'Cancelled.'})


class SmartDialog(Adw.Dialog):
    def __init__(self, window, disk):
        super().__init__(title='Disk health', content_width=520, content_height=560)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.page = Adw.PreferencesPage()
        self.status = Adw.StatusPage(icon_name='drive-harddisk-symbolic', title='Reading health data…',
                                     description='You may be asked for your password.')
        self.stack = Gtk.Stack()
        self.stack.add_named(self.status, 'status')
        self.stack.add_named(self.page, 'page')
        view.set_content(self.stack)
        self.set_child(view)
        self.disk = disk
        try:
            self.proc = HelperProcess(['smart', disk.path], True, self._event)
        except GLib.Error as e:
            self._fail(e.message)

    def _fail(self, msg):
        self.status.set_title('Could not read health data')
        self.status.set_description(msg)

    def _event(self, ev):
        if ev['event'] == 'smart':
            self._show(ev['data'])
        elif ev['event'] == 'error':
            self._fail(ev['message'])

    def _show(self, data):
        g = Adw.PreferencesGroup(title=self.disk.title())

        def row(title, value):
            r = Adw.ActionRow(title=title, subtitle=str(value), subtitle_selectable=True)
            g.add(r)
        passed = data.get('smart_status', {}).get('passed')
        row('Overall health', 'Passed' if passed else ('Failing. Back up now.' if passed is False else 'Unknown'))
        for key, title in (('model_name', 'Model'), ('serial_number', 'Serial number'), ('firmware_version', 'Firmware')):
            if data.get(key):
                row(title, data[key])
        temp = data.get('temperature', {}).get('current')
        if temp is not None:
            row('Temperature', f'{temp} °C')
        hours = data.get('power_on_time', {}).get('hours')
        if hours is not None:
            row('Powered on', f'{hours:,} hours (about {hours // 24:,} days)')
        cycles = data.get('power_cycle_count')
        if cycles is not None:
            row('Power cycles', f'{cycles:,}')
        nvme = data.get('nvme_smart_health_information_log')
        if nvme:
            row('Wear used', f'{nvme.get("percentage_used", "?")}%')
            row('Spare capacity left', f'{nvme.get("available_spare", "?")}%')
            row('Media errors', nvme.get('media_errors', '?'))
            row('Data written', human(nvme.get('data_units_written', 0) * 512000))
        self.page.add(g)
        attrs = data.get('ata_smart_attributes', {}).get('table', [])
        watch = [a for a in attrs if a.get('id') in (5, 187, 188, 196, 197, 198)]
        if watch:
            ag = Adw.PreferencesGroup(title='Attributes worth watching')
            for a in watch:
                r = Adw.ActionRow(title=a['name'].replace('_', ' '), subtitle=f'Raw value: {a["raw"]["value"]}')
                ag.add(r)
            self.page.add(ag)
        self.stack.set_visible_child_name('page')
