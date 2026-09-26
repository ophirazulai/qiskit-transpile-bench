"""Required evidence extensions named by a session.

A session can require that every cost bundle carry evidence checked by an extension, for
example the LSF monitor's cleanliness evidence. ``run.json:cost_evidence`` names it::

    {"contract": "qtb-lsf-monitor/1", "validator": "lsf.cost_evidence:validate", ...}

The requirement is recorded when the session is created, so removing fields from a bundle
cannot opt out of it. An extension that cannot be imported, or that implements another
contract, makes every bundle inadmissible: the evidence is refused, never accepted unchecked.
"""

import importlib

from qtb.errors import Incomplete


def validator(requirement):
    reference = requirement.get("validator") or ""
    module_name, _, attribute = reference.partition(":")
    try:
        module = importlib.import_module(module_name)
        function = getattr(module, attribute)
    except (ImportError, AttributeError, ValueError) as exc:
        raise Incomplete(
            f"The session's evidence extension {reference!r} is unavailable: {exc}"
        ) from exc
    contract = getattr(module, "CONTRACT", None)
    if contract != requirement.get("contract"):
        raise Incomplete(
            f"The evidence extension {reference} implements {contract}, "
            f"not the session's {requirement.get('contract')}"
        )
    return function


def check_cost_evidence(requirement, bundle, **context):
    """Raise ``Incomplete`` unless ``bundle`` satisfies the session's requirement, if any."""
    if requirement:
        validator(requirement)(bundle, requirement, **context)
