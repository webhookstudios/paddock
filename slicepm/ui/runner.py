"""Runs the privileged helper and turns its output into callbacks."""
import json
import os
import sys

from gi.repository import Gio, GLib

HELPER = '/usr/lib/slice/slice-helper'
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def helper_argv(args, elevated):
    if os.path.exists(HELPER):
        base = [HELPER] + args
        return (['pkexec'] + base) if elevated else base
    # running from a source checkout
    base = ['env', f'PYTHONPATH={ROOT}', sys.executable, '-m', 'slicepm.helper'] + args
    return (['pkexec'] + base) if elevated else base


class HelperProcess:
    """One helper run. `on_event` gets each JSON event as a dict."""

    def __init__(self, args, elevated, on_event, payload=None):
        self.on_event = on_event
        self.finished = False
        self.proc = Gio.Subprocess.new(
            helper_argv(args, elevated),
            Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        stdin = self.proc.get_stdin_pipe()
        if payload is not None:
            stdin.write_all(json.dumps(payload).encode(), None)
        stdin.close(None)
        self.stream = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self.stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._line)
        self.proc.wait_async(None, self._exited)
        self.got_end = False

    def _line(self, stream, res):
        try:
            line, _ = stream.read_line_finish_utf8(res)
        except GLib.Error:
            line = None
        if line is None:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            ev = {'event': 'log', 'line': line}      # pkexec or shell noise
        if ev.get('event') in ('done', 'error'):
            self.got_end = True
        self.on_event(ev)
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._line)

    def _exited(self, proc, res):
        try:
            proc.wait_finish(res)
        except GLib.Error:
            pass
        # Give the last lines a moment to be read, then make sure the caller hears an end.
        GLib.timeout_add(200, self._ensure_end)

    def _ensure_end(self):
        if not self.got_end:
            code = self.proc.get_exit_status()
            msg = ('Authentication was cancelled.' if code in (126, 127)
                   else f'The helper stopped unexpectedly (exit {code}).')
            self.got_end = True
            self.on_event({'event': 'error', 'message': msg})
        return False

    def cancel(self):
        if self.proc and not self.got_end:
            self.proc.force_exit()
