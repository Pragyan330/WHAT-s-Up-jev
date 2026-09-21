"""How much certainty an action has to earn before the agent takes it.

Two findings from the multi-step runs drive this.

First, one global threshold is the wrong shape. 0.70 on `target_present` was
simultaneously too high for a mid-wizard step (a correct `Recipient: Legal Team`
pick scored 0.65 and got blocked) and far too low for a terminal action. The
overlap is real and unfixable by moving the number: a correct act scored 0.65 and
a correct decline scored 0.62, three points apart, meaning opposite things.

Second, the Choice separates those two cases cleanly where the Noul cannot -
p=0.96 versus p=0.43. So the gate reads both signals. They arrive in the same
request, so the second one is free.

The tiers are a policy, not a measurement, and they live in code because that is
where consequences belong. The model judges what the screen affords; it does not
get to decide how bad being wrong would be.

  REVERSIBLE      navigation, opening a panel, advancing a step, typing in a
                  field. Wrong is cheap: click again, go back. Low bar.
  CONSEQUENTIAL   sends something, spends something, or tells other people.
                  Recoverable with effort and embarrassment. High bar.
  UNRECOVERABLE   destroys data. No bar is high enough on its own, so these are
                  never taken automatically: the agent declines and hands the
                  decision to a person, even when it is confident.

That last rule is the point. The failure mode worth engineering against is not a
wrong click on a reversible control, it is a marginal judgement call on a control
that deletes something.
"""

from __future__ import annotations

import re

REVERSIBLE = "reversible"
CONSEQUENTIAL = "consequential"
UNRECOVERABLE = "unrecoverable"

# Matched against the control's label, lowercased. Order matters: the most
# destructive pattern that matches wins, so "delete all" beats "send".
_UNRECOVERABLE = re.compile(
    r"\b(delete|wipe|purge|erase|destroy|shred|discard|revoke|deactivate|"
    r"uninstall|format\s+(?:disk|drive)|empty\s+(?:trash|bin)|"
    r"reset\s+to\s+defaults|remove\s+all|clear\s+all|drop\s+(?:table|database))\b")

_CONSEQUENTIAL = re.compile(
    r"\b(send|submit|publish|post|pay|buy|purchase|order|checkout|confirm|"
    r"archive|schedule|share|invite|sign\s*out|log\s*out|shut\s*down|restart|"
    r"transfer|withdraw|apply\s+changes|"
    # Web-specific commitments. These tell other people something or change an
    # account, and on a real page they sit one click from everything else.
    r"subscribe|unsubscribe|follow|report|flag|donate|join|comment|upload|"
    r"accept\s+all|agree\s+to\s+all|sign\s*in|log\s*in)\b")


# A leading navigational verb decides the tier, because the verb is the action and
# the rest of the label is just its subject. The first version of this file missed
# that and classified "Start Send Report" as consequential on the word "send" -
# but that button opens a wizard, it does not send anything, and treating it as a
# commitment blocked two multi-step tasks at step one. Opening the door to a
# dangerous room is not dangerous.
#
# This downgrade deliberately does NOT apply to UNRECOVERABLE. "Open Delete
# Records" really is only a dialog, but the cost of being wrong about that is
# asymmetric, so destructive wording keeps its tier wherever it appears.
_NAVIGATIONAL = re.compile(
    r"^\s*(open|start|show|view|go\s+to|goto|choose|select|pick|set|edit|"
    r"configure|customi[sz]e|preview|find|search|browse|expand|collapse|"
    r"back|next|previous|close|skip|attach|add|refresh|reload)\b")


def tier(label: str) -> str:
    text = (label or "").lower()
    if _UNRECOVERABLE.search(text):
        return UNRECOVERABLE
    if _NAVIGATIONAL.match(text):
        return REVERSIBLE
    if _CONSEQUENTIAL.search(text):
        return CONSEQUENTIAL
    return REVERSIBLE


# (min Choice probability, min target_present). Calibrated against the observed
# spread rather than guessed: correct reversible picks ran p>=0.78/present>=0.62,
# correct declines ran p<=0.44 or present<=0.52, and correct terminal sends ran
# p=1.00/present=0.96.
GATES: dict[str, tuple[float, float]] = {
    REVERSIBLE: (0.70, 0.55),
    CONSEQUENTIAL: (0.90, 0.80),
    UNRECOVERABLE: (0.98, 0.95),
}


def allows(label: str, p_target: float, target_present: float,
           allow_unrecoverable: bool = False) -> tuple[bool, str, str]:
    """Return (may_act, tier, why). `why` is for the log and for the human."""
    t = tier(label)
    p_min, present_min = GATES[t]

    if t == UNRECOVERABLE and not allow_unrecoverable:
        return (False, t,
                f"{label!r} destroys data; this is never taken automatically, "
                f"however confident the model is (p={p_target:.2f}, "
                f"present={target_present:.2f}). A person decides this one.")

    if p_target < p_min:
        return (False, t, f"choice p={p_target:.2f} below the {t} bar of {p_min}")
    if target_present < present_min:
        return (False, t,
                f"target_present={target_present:.2f} below the {t} bar of "
                f"{present_min}")
    return (True, t, f"{t}: p={p_target:.2f} >= {p_min}, "
                     f"present={target_present:.2f} >= {present_min}")
