"""The agent review's trusted Python code (standard library only).

Run only through an entry point in ``agent-review/bin/``, as
``/usr/bin/python3 -I -S -B <root>/bin/<name>`` (docs/adr/0004-python-runtime.md).

This file imports no submodule: an entry point's import closure must hold
only what it uses, so that the structure test can tell which entry points
reach the ``gh`` wrapper (and with it a token).
"""
