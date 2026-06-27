"""Path classification: stdlib vs. site-packages vs. project code.

These helpers decide whether a given file belongs to the standard library
(never stepped into), to an installed third-party package, or to the project
under inspection.
"""

import os
import sys
import sysconfig
import site


def _norm(path):
    """Normalize a path to a canonical absolute real path."""
    try:
        return os.path.realpath(os.path.abspath(path))
    except (OSError, ValueError):
        return os.path.abspath(path)


def _collect_stdlib():
    paths = set()
    try:
        paths.add(_norm(sysconfig.get_path("stdlib")))
    except (KeyError, OSError):
        pass
    try:
        paths.add(_norm(sysconfig.get_path("platstdlib")))
    except (KeyError, OSError):
        pass
    try:
        # e.g. /usr/lib/python3.10
        paths.add(_norm(os.path.dirname(os.__file__)))
    except (OSError, ValueError):
        pass
    return [p for p in paths if p]


def _collect_sitepackages():
    paths = set()
    try:
        for p in site.getsitepackages():
            paths.add(_norm(p))
    except (AttributeError, OSError):
        pass
    try:
        usp = site.getusersitepackages()
        if usp:
            paths.add(_norm(usp))
    except (AttributeError, OSError):
        pass
    return [p for p in paths if p]


STDLIB_PATHS = _collect_stdlib()
SITEPACKAGES_PATHS = _collect_sitepackages()


def is_stdlib(filename):
    """Return True for standard-library / built-in / frozen modules."""
    if not filename:
        return True
    if filename.startswith("<"):  # <frozen ...>, <string>, <builtin>
        return True
    f = _norm(filename)
    # Anything inside site-packages is third-party, not stdlib, even though it
    # may physically live under the python lib directory.
    for p in SITEPACKAGES_PATHS:
        if f == p or f.startswith(p + os.sep):
            return False
    for p in STDLIB_PATHS:
        if f == p or f.startswith(p + os.sep):
            return True
    return False


def is_sitepackage(filename):
    if not filename or filename.startswith("<"):
        return False
    f = _norm(filename)
    for p in SITEPACKAGES_PATHS:
        if f == p or f.startswith(p + os.sep):
            return True
    return False
