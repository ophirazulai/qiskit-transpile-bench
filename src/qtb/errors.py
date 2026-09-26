"""Errors crossing the harness's file and process boundaries."""


class HarnessError(Exception):
    """Invalid input, environment, protocol, or harness operation."""


class Unsupported(HarnessError):
    """An operation cannot be represented faithfully by this adapter."""


class Incomplete(HarnessError):
    """Evidence is absent or cannot support the requested inference."""


class Precondition(HarnessError):
    """A stage cannot start here and now: run an earlier stage, or use another host."""


class Usage(HarnessError):
    """The command line names something that cannot be done (exit 64)."""


class Contaminated(HarnessError):
    """Positively detected interference invalidated a measurement (stage exit 42).

    Not an ``Incomplete``: a panel or stage handler must never record it as unresolved
    evidence. The stage ends ``noisy``, which resumes; whoever orchestrates the stage
    decides whether to run it again. ``evidence`` carries the diagnostics.
    """

    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence or {}
