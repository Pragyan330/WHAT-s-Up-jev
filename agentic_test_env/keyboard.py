"""Keystrokes offered as numbered options, alongside the on-screen controls.

A click-only agent cannot reach anything that is not currently painted. With the
taskbar hidden there is no Start button to click, so the Windows key is the only
way in; a modal with no visible Cancel needs Esc; an unlabelled control needs Tab
to reach. So the option list gets a short keyboard section and the model picks a
keystroke the same way it picks a button.

Two things make keys different from controls, and both are safety-relevant.

**A keystroke has no target.** A click is addressed to an element; a key goes to
whatever holds focus. So every window-scoped key requires the target window to be
verifiably in the foreground first, and refuses otherwise - otherwise Ctrl+S
lands in whatever the person happened to be typing in.

**The tier is known, not guessed.** Elsewhere the risk tier is inferred from a
label, which has misfired three times. Here the action is fixed, so each key
carries its own tier. Enter is consequential because it activates whatever has
focus and the agent cannot see what that is; Ctrl+S commits to disk; Alt+F4
closes a window that may hold unsaved work.
"""

from __future__ import annotations

from dataclasses import dataclass

from risk import CONSEQUENTIAL, REVERSIBLE

# (label shown to the model, SendKeys sequence, tier, works without focus)
KEYS: list[tuple[str, str, str, bool]] = [
    ("press the Windows key to open the Start menu", "{Win}", REVERSIBLE, True),
    ("press Escape to dismiss or cancel", "{Esc}", REVERSIBLE, False),
    ("press Tab to move focus to the next control", "{Tab}", REVERSIBLE, False),
    ("press Shift+Tab to move focus to the previous control",
     "{Shift}{Tab}", REVERSIBLE, False),
    ("press the Down arrow", "{Down}", REVERSIBLE, False),
    ("press the Up arrow", "{Up}", REVERSIBLE, False),
    ("press Ctrl+Tab to switch to the next tab", "{Ctrl}{Tab}", REVERSIBLE, False),
    ("press Ctrl+A to select all", "{Ctrl}a", REVERSIBLE, False),
    ("press Ctrl+F to open find", "{Ctrl}f", REVERSIBLE, False),
    ("press Ctrl+Z to undo the last change", "{Ctrl}z", REVERSIBLE, False),
    ("press Ctrl+C to copy", "{Ctrl}c", REVERSIBLE, False),
    ("press Ctrl+V to paste", "{Ctrl}v", REVERSIBLE, False),
    # Activates whatever currently has focus, which the agent cannot see.
    ("press Enter to activate the focused control", "{Enter}", CONSEQUENTIAL, False),
    ("press Ctrl+S to save", "{Ctrl}s", CONSEQUENTIAL, False),
    ("press Alt+F4 to close the active window", "{Alt}{F4}", CONSEQUENTIAL, False),
]


@dataclass
class KeyControl:
    """Duck-typed to match uia_scrape.Element so agent.decide() is unchanged."""
    number: int
    label: str
    sequence: str
    tier: str
    global_ok: bool
    kind: str = "key"
    window: str = "keyboard"

    def describe(self) -> str:
        return f"{self.label} (keyboard shortcut)"


def key_controls(start_number: int) -> list[KeyControl]:
    """Numbered continuing on from the window's controls."""
    return [KeyControl(number=start_number + i, label=label, sequence=seq,
                       tier=tier, global_ok=global_ok)
            for i, (label, seq, tier, global_ok) in enumerate(KEYS)]
