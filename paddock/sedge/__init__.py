"""Sedge design kit for GTK 4 apps written in Python.

Call apply() once at startup, after Gtk is ready (for example in the
application's startup handler). It loads the Sedge fonts for this app only
and adds the shared stylesheet. An app's own stylesheet still wins, because
the kit is added one step below the normal application priority.

This folder is copied in by sedge-design/sync.py. Change the kit there.
"""
import os

import gi

gi.require_version('Gdk', '4.0')
gi.require_version('Gtk', '4.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Gdk, Gtk, PangoCairo  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = ('YoungSerif-Regular.ttf', 'PublicSans[wght].ttf', 'PublicSans-Italic[wght].ttf')
_applied = False


def _add_fonts():
    fontmap = PangoCairo.FontMap.get_default()
    if not hasattr(fontmap, 'add_font_file'):
        return  # Pango older than 1.56: the system fonts are used instead.
    for name in FONTS:
        path = os.path.join(HERE, 'fonts', name)
        if os.path.exists(path):
            try:
                fontmap.add_font_file(path)
            except Exception:  # A missing or broken font must never stop the app.
                pass


def _sheet(path):
    provider = Gtk.CssProvider()
    provider.load_from_path(path)
    return provider


def _follow_dark(display, provider):
    """Adds the dark colors while the app is dark: libadwaita's choice when it is in use,
    otherwise GTK's color-scheme setting."""
    priority = Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION - 1
    state = {'on': False}

    def set_dark(dark):
        if dark == state['on']:
            return
        state['on'] = dark
        if dark:
            Gtk.StyleContext.add_provider_for_display(display, provider, priority)
        else:
            Gtk.StyleContext.remove_provider_for_display(display, provider)

    try:
        gi.require_version('Adw', '1')
        from gi.repository import Adw
        manager = Adw.StyleManager.get_for_display(display)
        manager.connect('notify::dark', lambda m, _p: set_dark(m.get_dark()))
        set_dark(manager.get_dark())
        return
    except (ImportError, ValueError):
        pass
    settings = Gtk.Settings.get_for_display(display)
    if settings.find_property('gtk-interface-color-scheme') is not None:
        def scheme(*_):
            set_dark(settings.props.gtk_interface_color_scheme == Gtk.InterfaceColorScheme.DARK)
        settings.connect('notify::gtk-interface-color-scheme', scheme)
        scheme()
    else:
        def prefer(*_):
            set_dark(bool(settings.props.gtk_application_prefer_dark_theme))
        settings.connect('notify::gtk-application-prefer-dark-theme', prefer)
        prefer()


def apply(display=None):
    """Load the Sedge fonts and stylesheet for this app. Safe to call more than once."""
    global _applied
    if _applied:
        return
    display = display or Gdk.Display.get_default()
    if display is None:
        return
    _add_fonts()
    Gtk.StyleContext.add_provider_for_display(
        display, _sheet(os.path.join(HERE, 'sedge.css')), Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION - 1)
    _follow_dark(display, _sheet(os.path.join(HERE, 'sedge-dark.css')))
    _applied = True
