"""The coloured bar that shows how a disk is divided."""
import math

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GObject, Gtk

from .. import fs as fsinfo
from ..model import MiB, human

COLORS = {
    'ext4': (0.21, 0.52, 0.89), 'ext3': (0.21, 0.52, 0.89), 'ext2': (0.21, 0.52, 0.89),
    'ntfs': (0.57, 0.25, 0.67), 'vfat': (0.90, 0.65, 0.04), 'exfat': (0.78, 0.27, 0.0),
    'btrfs': (0.15, 0.64, 0.41), 'xfs': (0.11, 0.44, 0.85), 'swap': (0.60, 0.42, 0.27),
    None: (0.47, 0.46, 0.48),
}
OTHER = (0.47, 0.46, 0.48)


def color_for(fs):
    return COLORS.get(fs, OTHER)


class DiskMap(Gtk.DrawingArea):
    __gsignals__ = {'picked': (GObject.SignalFlags.RUN_FIRST, None, (str, int))}

    def __init__(self):
        super().__init__()
        self.set_content_height(96)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.disk = None
        self.selected = None       # ('p'|'f', start)
        self.hover = None
        self.segs = []
        click = Gtk.GestureClick()
        click.connect('pressed', self._pressed)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect('motion', self._motion)
        motion.connect('leave', lambda *_: self._set_hover(None))
        self.add_controller(motion)
        self.set_cursor_from_name('pointer')
        self.set_focusable(True)
        self.update_property([Gtk.AccessibleProperty.LABEL], ['Disk layout'])

    def set_disk(self, disk, selected=None):
        self.disk, self.selected = disk, selected
        self.queue_draw()

    # ---- layout --------------------------------------------------------------

    def _segments(self, width):
        d = self.disk
        if not d or d.size <= 0:
            return []
        items = []
        for p in d.sorted_parts():
            if p.role != 'logical':
                items.append(('p', p.start, p.size, p))
        for start, size in d.free_regions():
            items.append(('f', start, size, None))
        items.sort(key=lambda s: s[1])
        gap = 3
        usable = max(1, width - gap * (len(items) - 1))
        min_w = 34
        total = sum(i[2] for i in items) or 1
        widths = [max(min_w, usable * i[2] / total) for i in items]
        # Shrink the large ones so the minimums fit.
        excess = sum(widths) - usable
        if excess > 0:
            big = [k for k, w in enumerate(widths) if w > min_w * 2]
            room = sum(widths[k] - min_w for k in big) or 1
            for k in big:
                widths[k] -= excess * (widths[k] - min_w) / room
        x, segs = 0, []
        for item, w in zip(items, widths):
            segs.append((x, w, item))
            x += w + gap
        return segs

    def _draw(self, area, cr, width, height):
        dark = Adw.StyleManager.get_default().get_dark()
        fg = (1, 1, 1) if dark else (0.1, 0.1, 0.12)
        self.segs = self._segments(width)
        if not self.disk:
            return
        if not self.segs:
            self._text(cr, 'Empty disk', width / 2, height / 2, fg, 13, center=True, alpha=0.6)
            return
        top, h = 8, height - 16
        for x, w, (kind, start, size, p) in self.segs:
            sel = self.selected == (kind, start)
            hov = self.hover == (kind, start)
            self._round(cr, x, top, w, h, 9)
            if kind == 'f':
                base = (0.5, 0.5, 0.5)
                cr.set_source_rgba(*base, 0.14 if not hov else 0.22)
                cr.fill_preserve()
                cr.set_source_rgba(*base, 0.7)
                cr.set_dash([5, 4])
                cr.set_line_width(1.2)
                cr.stroke()
                cr.set_dash([])
                self._labels(cr, x, top, w, h, 'Unallocated', human(size), fg, 0.75)
            else:
                r, g, b = color_for(p.fs)
                cr.set_source_rgba(r, g, b, 0.38)
                cr.fill_preserve()
                cr.save()
                cr.clip_preserve()
                if p.used is not None and p.size:
                    frac = min(1.0, p.used / p.size)
                    cr.rectangle(x, top, w * frac, h)
                    cr.set_source_rgba(r, g, b, 0.95)
                    cr.fill()
                else:
                    cr.rectangle(x, top, w, h)
                    cr.set_source_rgba(r, g, b, 0.55)
                    cr.fill()
                cr.restore()
                title = p.title() if not p.new else 'New'
                sub = f'{fsinfo.FILESYSTEMS.get(p.fs, {}).get("title", p.fs or "Unformatted")} · {human(p.size)}'
                self._labels(cr, x, top, w, h, p.label or title, sub, (1, 1, 1), 1.0, shadow=True)
                if p.new or p.changed:
                    cr.arc(x + w - 10, top + 10, 4, 0, 2 * math.pi)
                    cr.set_source_rgba(1, 1, 1, 0.95)
                    cr.fill()
            if sel or hov:
                self._round(cr, x, top, w, h, 9)
                cr.set_line_width(2.5 if sel else 1.5)
                if sel:
                    cr.set_source_rgba(0.21, 0.52, 0.89, 1)
                else:
                    cr.set_source_rgba(*fg, 0.35)
                cr.stroke()

    def _round(self, cr, x, y, w, h, r):
        r = min(r, w / 2, h / 2)
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def _text(self, cr, text, x, y, color, size, center=False, bold=False, alpha=1.0):
        cr.select_font_face('Sans', 0, 1 if bold else 0)
        cr.set_font_size(size)
        ext = cr.text_extents(text)
        cr.move_to(x - (ext.width / 2 if center else 0), y)
        cr.set_source_rgba(*color, alpha)
        cr.show_text(text)

    def _fit(self, cr, text, width, size, bold):
        cr.select_font_face('Sans', 0, 1 if bold else 0)
        cr.set_font_size(size)
        while text and cr.text_extents(text).x_advance > width:
            text = text[:-1]
            if len(text) <= 1:
                return ''
        return text

    def _labels(self, cr, x, y, w, h, title, sub, color, alpha, shadow=False):
        pad = 10
        avail = w - pad * 2
        if avail < 24:
            return
        t = self._fit(cr, title, avail, 13, True)
        s = self._fit(cr, sub, avail, 11, False)
        by = y + h / 2 - (4 if s else -4)
        if t:
            if shadow:
                self._text(cr, t, x + pad + 0.5, by + 0.8, (0, 0, 0), 13, bold=True, alpha=0.25)
            self._text(cr, t, x + pad, by, color, 13, bold=True, alpha=alpha)
        if s:
            if shadow:
                self._text(cr, s, x + pad + 0.5, by + 17.8, (0, 0, 0), 11, alpha=0.25)
            self._text(cr, s, x + pad, by + 17, color, 11, alpha=alpha * 0.85)

    # ---- pointer ---------------------------------------------------------------

    def _hit(self, x):
        for sx, w, item in self.segs:
            if sx <= x <= sx + w:
                return (item[0], item[1])
        return None

    def _pressed(self, gesture, n, x, y):
        hit = self._hit(x)
        if hit:
            self.emit('picked', hit[0], hit[1])

    def _motion(self, ctrl, x, y):
        self._set_hover(self._hit(x))

    def _set_hover(self, hit):
        if hit != self.hover:
            self.hover = hit
            self.queue_draw()
