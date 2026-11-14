"""No-op probe of the Python runtime: ``bin/probe``.

Prints one line, so the self-test can compare the host and the AWF sandbox:

    agent-review-probe python=3.12.3 executable=/usr/bin/python3 isolated=1 ignore_environment=1
    no_user_site=1 no_site=1 dont_write_bytecode=1 safe_path=1 site=0 stdlib_only=1 lib=<root>/lib

(one line in the output). ``site=0``: neither ``site`` nor
``sitecustomize`` is loaded. ``stdlib_only=1``: ``sys.path`` holds the
trusted ``lib`` and the standard library's own entries only, and ``json``
and this package were loaded from them. It imports the package's modules
except ``gh``, to show that their standard-library imports resolve where it
runs. The stub harness runs it on the host (``install.sh``, in ``prepare``)
and in the sandbox (``run.sh``).
"""

import json
import os
import sys

import agent_review
from agent_review import files, runner, validate, workflow

# Imported only to show that they import here; never 'gh'.
MODULES = (files, runner, validate, workflow)
FLAGS = ("isolated", "ignore_environment", "no_user_site", "no_site", "dont_write_bytecode", "safe_path")


def lib_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.realpath(agent_review.__file__)))


def stdlib_paths() -> list[str]:
    major, minor = sys.version_info[:2]
    base = os.path.join(sys.base_prefix, "lib")
    return [
        os.path.join(base, f"python{major}{minor}.zip"),
        os.path.join(base, f"python{major}.{minor}"),
        os.path.join(base, f"python{major}.{minor}", "lib-dynload"),
    ]


def stdlib_only(lib: str) -> bool:
    stdlib = stdlib_paths()
    origins_ok = os.path.realpath(json.__file__).startswith(stdlib[1] + os.sep) and os.path.realpath(
        agent_review.__file__
    ).startswith(lib + os.sep)
    return origins_ok and sys.path == [lib, *stdlib]


def line() -> str:
    lib = lib_dir()
    flags = " ".join(f"{name}={int(getattr(sys.flags, name))}" for name in FLAGS)
    site = int("site" in sys.modules or "sitecustomize" in sys.modules)
    version = "{}.{}.{}".format(*sys.version_info[:3])
    return (
        f"agent-review-probe python={version} executable={sys.executable} {flags}"
        f" site={site} stdlib_only={int(stdlib_only(lib))} lib={lib}"
    )


def main() -> int:
    sys.stdout.buffer.write((line() + "\n").encode("utf-8", errors="strict"))
    sys.stdout.buffer.flush()
    return 0
