"""Domain exception for the depot subsystem.

Every business-rule violation anywhere in the ``depot`` package is raised as
a single ``DepotError``, distinguished only by a stable, three-digit error
code (``"DEP-###"``).  Callers (and, importantly, the automated tests that
exercise this fixture) are expected to match on ``error.code`` rather than
on the free-text message, so the message is free to be reworded without
breaking anything; the code is the contract.
"""


class DepotError(Exception):
    """Raised for any domain-rule violation inside the depot package.

    Args:
        code: a literal string of the form ``"DEP-###"`` identifying the
            specific rule that was violated. Codes are assigned once and
            never reused for a different rule.
        message: a short, human-readable explanation of what went wrong.
    """

    def __init__(self, code: str, message: str) -> None:
        """Store the error code and message and build the exception text."""
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")
