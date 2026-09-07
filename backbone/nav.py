"""nav.py - app-wide navigation breadcrumb and the quit-to-terminal signal,
shared by every back* tool that uses backbone's prompt widgets.
"""
NAV_STACK = ["Home"]


class QuitToTerminal(BaseException):
    """Raised to unwind the entire menu stack and exit straight to the
    terminal. Derives from BaseException (not Exception) so it bypasses
    ``except Exception`` handlers in editors/widgets and propagates cleanly
    up to the app's main(), where the alt-screen is restored in a finally.

    `q` quits an app from anywhere by raising this on the spot - there is
    deliberately no "leave this widget and quit later" flag: `q` is never a
    way out of a widget; Esc (or <-/b where a widget has no other use for
    them) is what backs out.
    """
