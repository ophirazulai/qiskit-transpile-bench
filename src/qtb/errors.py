"""Errors crossing the harness's file and process boundaries."""


class HarnessError(Exception):
    """Invalid input, environment, protocol, or harness operation."""


class Unsupported(HarnessError):
    """An operation cannot be represented faithfully by this adapter."""


class Incomplete(HarnessError):
    """Evidence is absent or cannot support the requested inference."""
