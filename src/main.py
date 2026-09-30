import asyncio
import os
import shutil
import signal
import sys
import threading
import traceback
from gettext import gettext as _

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.events import GLibEventLoopPolicy

asyncio.set_event_loop_policy(GLibEventLoopPolicy())

from gi.repository import Adw, Gio, GLib

from .constants import NAME, set_version
from .integrations import Suwayomi
from .widgets import KaghezWindow, build_shortcuts
from .widgets.preferences import KaghezPreferences
from .widgets.setup import SetupWindow

if sys.platform == 'win32':
    from . import winproc

APPLICATION_ID = 'com.rini.kaghez'
JAR_NAME = 'Suwayomi-Server.jar'
LOCAL_URL = 'http://localhost:4567'

READY_TIMEOUT = 60.0
POLL_INTERVAL = 0.5
SHUTDOWN_TIMEOUT = 5.0

SERVER_JVM_ARGS = (
    '-Dsuwayomi.tachidesk.config.server.systemTrayEnabled=false',
    '-Dsuwayomi.tachidesk.config.server.initialOpenInBrowserEnabled=false',
    '-Dsuwayomi.tachidesk.config.server.kcefEnabled=false',
)


def get_pkgdatadir() -> str:
    if getattr(sys, 'frozen', False):
        # PyInstaller keeps our modules inside its archive, so __file__
        # doesn't point into the installed tree. The data lives where
        # kaghez.in's frozen branch puts pkgdatadir.
        return os.path.join(sys._MEIPASS, 'share', 'kaghez')
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LocalServer:
    def __init__(self) -> None:
        # A subprocess.Popen on Windows, see winproc.
        self.proc: Gio.Subprocess | None = None
        self.pump: asyncio.Task | None = None
        self.exited = False

    @property
    def running(self) -> bool:
        return self.proc is not None and not self.exited

    @staticmethod
    def jar_path() -> str | None:
        path = os.path.join(get_pkgdatadir(), JAR_NAME)
        return path if os.path.isfile(path) else None

    @staticmethod
    def java_path() -> str | None:
        """Prefer a JRE shipped next to the jar (the Windows build bundles
        one); otherwise rely on `java` from PATH, like the Flatpak does."""
        java_name = 'java.exe' if sys.platform == 'win32' else 'java'
        # Not named "jre": the server's class scanner (ClassGraph) treats the
        # parent of a "jre" folder as part of the JDK and skips the jar
        # sitting next to it, and the server then fails to start.
        bundled = os.path.join(get_pkgdatadir(), 'java', 'bin', java_name)
        return bundled if os.path.isfile(bundled) else shutil.which('java')

    def start(self) -> bool:
        if self.running:
            return True

        jar = self.jar_path()
        if not jar:
            print('[server] bundled Suwayomi jar not found')
            return False

        java = self.java_path()
        if not java:
            print('[server] Java runtime not found in PATH')
            return False

        if sys.platform == 'win32':
            try:
                self.proc = winproc.spawn_server(
                    [java, *SERVER_JVM_ARGS, '-jar', jar],
                    lambda proc: GLib.idle_add(self._on_win_exit, proc))
            except OSError as e:
                print(f'[server] failed to start: {e}')
                self.proc = None
                return False
            self.exited = False
            return True

        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        try:
            self.proc = launcher.spawnv([java, *SERVER_JVM_ARGS, '-jar', jar])
        except GLib.Error as e:
            print(f'[server] failed to start: {e.message}')
            self.proc = None
            return False

        self.exited = False
        self.proc.wait_async(None, self._on_exit)
        self.pump = asyncio.create_task(self._pump_output(self.proc))
        return True

    def stop(self) -> None:
        if self.pump:
            self.pump.cancel()
            self.pump = None

        proc, self.proc = self.proc, None
        if proc is None or self.exited:
            self.exited = False
            return

        if sys.platform == 'win32':
            winproc.stop_server(proc)
            self.exited = False
            return

        proc.send_signal(signal.SIGTERM)
        cancellable = Gio.Cancellable()
        timer = threading.Timer(SHUTDOWN_TIMEOUT, cancellable.cancel)
        timer.start()
        try:
            proc.wait(cancellable)
        except GLib.Error:
            print('[server] did not exit in time, killing it')
            proc.force_exit()
        finally:
            timer.cancel()
        self.exited = False

    def _on_exit(self, proc: Gio.Subprocess, result: Gio.AsyncResult) -> None:
        try:
            proc.wait_finish(result)
            if proc.get_if_exited():
                print(f'[server] exited with status {proc.get_exit_status()}')
            elif proc.get_if_signaled():
                print(f'[server] terminated by signal {proc.get_term_sig()}')
        except GLib.Error as e:
            print(f'[server] wait failed: {e.message}')
        if proc is self.proc:
            self.exited = True

    def _on_win_exit(self, proc) -> bool:
        print(f'[server] exited with status {proc.returncode}')
        if proc is self.proc:
            self.exited = True
        return GLib.SOURCE_REMOVE

    @staticmethod
    async def _pump_output(proc: Gio.Subprocess) -> None:
        stream = Gio.DataInputStream.new(proc.get_stdout_pipe())
        try:
            while True:
                line, _length = await stream.read_line_async(GLib.PRIORITY_DEFAULT)
                if line is None:
                    break
                print(f'[suwayomi] {line.decode(errors="replace")}')
        except GLib.Error as e:
            print(f'[suwayomi] {e.message}')


class KaghezApplication(Adw.Application):

    def __init__(self, version: str) -> None:
        flags = Gio.ApplicationFlags.DEFAULT_FLAGS
        if sys.platform == 'win32':
            # Single-instance mode goes through D-Bus, which Windows
            # doesn't have without shipping and autolaunching gdbus.exe.
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APPLICATION_ID,
                         flags=flags,
                         resource_base_path='/com/rini/kaghez')
        self.version = version
        self.settings = Gio.Settings(schema_id=APPLICATION_ID)
        self.suwayomi = Suwayomi()
        self.server = LocalServer()

        self.create_action('quit', lambda *_: self.quit(), ['<control>q'])
        self.create_action('preferences', self.on_preferences_action, ['<control>comma'])
        self.create_action('shortcuts', self.on_shortcuts_action,
                           ['<control>question', '<control>slash'])
        self.create_action('about', self.on_about_action)
        self.create_action('change_instance', self.on_change_instance_action)


    def do_activate(self) -> None:
        if win := self.props.active_window:
            win.present()
            return

        mode = self.settings.get_string('suwayomi-mode')
        if mode not in ('local', 'remote'):
            self.show_setup_window()
            return

        self.hold()
        task = asyncio.create_task(
            self.launch_or_setup(mode, self.settings.get_string('suwayomi-url')))
        task.add_done_callback(self.log_task_error)

    def do_shutdown(self) -> None:
        self.server.stop()
        self.suwayomi.shutdown()
        Adw.Application.do_shutdown(self)

    @staticmethod
    def log_task_error(task: asyncio.Task) -> None:
        if not task.cancelled() and (exc := task.exception()):
            traceback.print(_exception(exc))

    async def launch_or_setup(self, mode: str, url: str) -> None:
        try:
            if not await self.launch(mode, url):
                self.show_setup_window(
                    error=_("Couldn't reconnect. Check your setup and try again."))
        finally:
            self.release()

    async def launch(self, mode: str, url: str) -> bool:
        """Connect to a local or remote server and open the main window."""
        local = mode == 'local'
        if local and not self.server.start():
            return False
        if not local and not url:
            return False

        self.suwayomi.reconnect(LOCAL_URL if local else url)

        if not await self.wait_for_server():
            if local:
                self.server.stop()
            return False

        self.settings.set_string('suwayomi-mode', mode)
        self.settings.set_string('suwayomi-url', '' if local else url)
        KaghezWindow(application=self).present()
        return True

    async def wait_for_server(self) -> bool:
        """Poll until the server answers, it dies, or the timeout expires.

        The GraphQL session is reused across polls: connection errors surface
        per request, so the same session recovers as soon as the server is up.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READY_TIMEOUT
        while loop.time() < deadline:
            if self.server.proc is not None and self.server.exited:
                print('[server] process exited before becoming ready')
                return False
            if await self.suwayomi.getServerSettings():
                return True
            await asyncio.sleep(POLL_INTERVAL)
        print(f'[server] not ready after {READY_TIMEOUT:.0f}s')
        return False


    def show_setup_window(self, error: str | None = None) -> None:
        SetupWindow(application=self, error=error).present()

    def on_change_instance_action(self, *args) -> None:
        self.server.stop()
        if win := self.props.active_window:
            for dialog in list(win.get_dialogs()):
                dialog.force_close()
            win.close()
        self.show_setup_window()

    def on_about_action(self, *args) -> None:
        Adw.AboutDialog(
            application_name=NAME,
            application_icon=APPLICATION_ID,
            developer_name='Riyan Parvez',
            version=self.version,
            translator_credits=_('translator-credits'),
            developers=['Riyan Parvez (rini)'],
            designers=['Riyan Parvez (rini)'],
            copyright='© 2026 Riyan Parvez',
            issue_url='https://github.com/riyaniii/Kaghez/issues',
            license='GPL-3.0-or-later',
            website='https://github.com/riyaniii/Kaghez',
        ).present(self.props.active_window)

    def on_preferences_action(self, *args) -> None:
        KaghezPreferences().present(self.props.active_window)

    def on_shortcuts_action(self, *args) -> None:
        build_shortcuts().present(self.props.active_window)

    def create_action(self, name, callback, shortcuts=None, parameter_type=None) -> None:
        action = Gio.SimpleAction.new(name, parameter_type)
        action.connect('activate', callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f'app.{name}', shortcuts)


def main(version: str) -> int:
    set_version(version)
    return KaghezApplication(version).run(sys.argv)

