import sys

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio

from .ui.window import PaddockWindow

APP_ID = 'io.github.webhookstudios.Paddock'


class PaddockApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.win = None

    def _window(self):
        if not self.win:
            self.win = PaddockWindow(self)
        return self.win

    def do_startup(self):
        Adw.Application.do_startup(self)
        from . import sedge
        sedge.apply()

    def do_activate(self):
        self._window().present()

    def do_open(self, files, n_files, hint):
        win = self._window()
        for f in files:
            if f.get_path():
                win.state.add_image(f.get_path())
        win.refresh()
        win.present()


def main():
    return PaddockApp().run(sys.argv)
