"""Central command priority and voice-text validation for the Smart Car."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class CommandSender(Protocol):
    def send(self, command: str) -> None: ...


class Direction(str, Enum):
    FORWARD = "FORWARD"
    BACKWARD = "BACKWARD"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    STOP = "STOP"


@dataclass(frozen=True)
class CommandResult:
    accepted: bool
    direction: Direction | None
    arduino_command: str | None
    reason: str


def parse_voice_command(text: str) -> Direction | None:
    """Convert a short natural-language instruction into one safe direction."""
    words = set(re.findall(r"[a-z]+", text.lower()))
    normalized = " ".join(re.findall(r"[a-z]+", text.lower()))

    # STOP always wins, even if another direction is present in the sentence.
    if words & {"stop", "halt", "freeze", "emergency"}:
        return Direction.STOP
    if "do not move" in normalized or "dont move" in normalized:
        return Direction.STOP
    if re.search(r"\b(?:do not|don t|dont|never|no)\b", normalized):
        return None

    matches: set[Direction] = set()
    if words & {"forward", "ahead", "straight"}:
        matches.add(Direction.FORWARD)
    if words & {"back", "backward", "backwards", "reverse"}:
        matches.add(Direction.BACKWARD)
    if "left" in words:
        matches.add(Direction.LEFT)
    if "right" in words:
        matches.add(Direction.RIGHT)

    # Refuse an ambiguous phrase instead of guessing a motor command.
    if len(matches) != 1:
        return None
    return matches.pop()


class CommandManager:
    """Coordinate manual/voice commands, AI recovery, and emergency priority."""

    def __init__(self, arduino: CommandSender) -> None:
        self.arduino = arduino
        self.recovery_active = False
        self.emergency_latched = False

    def begin_recovery(self) -> CommandResult:
        if self.emergency_latched:
            return self._rejected("Emergency STOP is latched.")
        self.arduino.send("AI_START")
        self.recovery_active = True
        return self._accepted(None, "AI_START", "Recovery mode started safely.")

    def send_ai_direction(self, direction: Direction) -> CommandResult:
        if self.emergency_latched:
            return self._rejected("Emergency STOP overrides AI recovery.", direction)
        if not self.recovery_active:
            return self._rejected("AI recovery is not active.", direction)

        command = {
            Direction.FORWARD: "AI_FORWARD",
            Direction.BACKWARD: "AI_BACKWARD",
            Direction.LEFT: "AI_LEFT",
            Direction.RIGHT: "AI_RIGHT",
            Direction.STOP: "AI_STOP",
        }[direction]
        self.arduino.send(command)
        return self._accepted(direction, command, "AI command accepted.")

    def finish_recovery(self) -> CommandResult:
        if self.emergency_latched:
            return self._rejected("Cannot resume while emergency STOP is latched.")
        if not self.recovery_active:
            return self._rejected("AI recovery is not active.")

        self.arduino.send("AI_RESUME")
        self.recovery_active = False
        return self._accepted(None, "AI_RESUME", "Previous mode resumed.")

    def send_voice_text(self, text: str) -> CommandResult:
        direction = parse_voice_command(text)
        if direction is None:
            return self._rejected("Voice instruction was unclear or ambiguous.")

        if direction is Direction.STOP:
            self.arduino.send("EMERGENCY_STOP")
            self.emergency_latched = True
            self.recovery_active = False
            return self._accepted(
                direction,
                "EMERGENCY_STOP",
                "Emergency STOP accepted and latched.",
            )

        if self.emergency_latched:
            return self._rejected(
                "Movement refused until emergency STOP is deliberately cleared.",
                direction,
            )

        command = direction.value
        self.arduino.send(command)
        self.recovery_active = False
        return self._accepted(direction, command, "Voice movement accepted.")

    def clear_emergency(self) -> CommandResult:
        # Clearing the latch does not move the car; it remains stopped.
        self.arduino.send("CLEAR_EMERGENCY")
        self.emergency_latched = False
        return self._accepted(None, "CLEAR_EMERGENCY", "Emergency latch cleared; car stays stopped.")

    @staticmethod
    def _accepted(
        direction: Direction | None,
        command: str,
        reason: str,
    ) -> CommandResult:
        return CommandResult(True, direction, command, reason)

    @staticmethod
    def _rejected(
        reason: str,
        direction: Direction | None = None,
    ) -> CommandResult:
        return CommandResult(False, direction, None, reason)
