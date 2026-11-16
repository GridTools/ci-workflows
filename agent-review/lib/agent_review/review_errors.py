"""The error that stops ``run-review``."""


class Fatal(Exception):
    """Stops run-review with exit code 1; the message is printed as 'run-review: <message>'."""
