# -*- coding: utf-8 -*-
"""Shared setup for the headless test suites.

Importing this module puts Qt in offscreen mode, makes dcm_align_app importable
from the parent directory, points the app's auto-save at a throwaway file so the
real dcm_config.json is never touched, and silences the modal dialogs.
"""
import os
import sys
import tempfile

# Several app strings the suites echo contain '→' (the arrow in the substep
# labels). A default Windows console is cp437/cp1252, so printing one raises
# UnicodeEncodeError from inside Report.check and kills the run mid-suite --
# losing every result after that point rather than reporting a failure. Force
# UTF-8 on our own streams so the suites are runnable without the caller having
# to remember PYTHONIOENCODING.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass   # already redirected to something without reconfigure()

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import dcm_align_app as app  # noqa: E402

# Never let a test write to the operator's real configuration.
TMPDIR = tempfile.mkdtemp(prefix="dcm_test_")
app.AUTO_CONFIG_PATH = os.path.join(TMPDIR, "test_config.json")
# Same isolation for the run lock. Without this a dcm_run.lock left at the repo
# root by a crashed process would look like a live lock to the next suite, and
# the stale-lock branch would then depend on whatever silence_dialogs() happens
# to answer -- a test outcome decided by a stray file. Both functions resolve
# `path or RUN_LOCK_PATH` at call time, so rebinding the module global is enough.
app.RUN_LOCK_PATH = os.path.join(TMPDIR, "test_run.lock")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from PyQt6.QtCore import QTimer, QEventLoop           # noqa: E402


def silence_dialogs(question=QMessageBox.StandardButton.Yes):
    """Auto-answer the modal prompts so a run never blocks on one."""
    QMessageBox.question = staticmethod(lambda *a, **k: question)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QMessageBox.information = staticmethod(lambda *a, **k: None)


def qapp():
    return QApplication.instance() or QApplication(sys.argv)


def pump(predicate, timeout_ms=15000):
    """Spin the Qt event loop until predicate() is true, or time out.

    Returns whether the predicate came true.
    """
    loop = QEventLoop()
    state = {"done": False}
    tick = QTimer()
    tick.setInterval(20)
    guard = QTimer()
    guard.setSingleShot(True)

    def check():
        if predicate():
            state["done"] = True
            loop.quit()

    tick.timeout.connect(check)
    guard.timeout.connect(loop.quit)
    tick.start()
    guard.start(timeout_ms)
    loop.exec()
    tick.stop()
    guard.stop()
    return state["done"]


class Report:
    """Collects pass/fail lines and sets the process exit code."""

    def __init__(self, title):
        self.failures = []
        print(title)
        print("-" * len(title))

    def check(self, ok, msg):
        print(("PASS  " if ok else "FAIL  ") + msg)
        if not ok:
            self.failures.append(msg)
        return ok

    def fail(self, msg):
        self.check(False, msg)

    def finish(self):
        print()
        code = 0
        if self.failures:
            print("FAILURES:")
            for f in self.failures:
                print("  -", f)
            code = 1
        else:
            print("ALL TESTS PASSED")
        _hard_exit(code)


def _hard_exit(code):
    """Exit immediately, bypassing interpreter shutdown.

    A plain sys.exit() can hang here: the tests leave a live QApplication and,
    with pyepics loaded, open channel-access connections whose teardown does not
    always complete. The result has already been printed, so there is nothing
    to lose by skipping atexit and thread joins.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
