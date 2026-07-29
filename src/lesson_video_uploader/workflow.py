from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class WorkflowState(StrEnum):
    PERIOD_SELECTED = "PERIOD_SELECTED"
    PREFLIGHT_RUNNING = "PREFLIGHT_RUNNING"
    PREFLIGHT_BLOCKED = "PREFLIGHT_BLOCKED"
    PREFLIGHT_PASSED = "PREFLIGHT_PASSED"
    MATCHING_RUNNING = "MATCHING_RUNNING"
    RESOLUTION_REQUIRED = "RESOLUTION_REQUIRED"
    BATCH_READY = "BATCH_READY"
    REVALIDATION_RUNNING = "REVALIDATION_RUNNING"
    BATCH_REVALIDATION_REQUIRED = "BATCH_REVALIDATION_REQUIRED"
    SENDING = "SENDING"
    SEND_PAUSED = "SEND_PAUSED"
    SEND_FAILED = "SEND_FAILED"
    COMPLETED = "COMPLETED"


class WorkflowTransitionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorkflowButtonState:
    preflight_enabled: bool
    matching_enabled: bool
    resolve_enabled: bool
    send_enabled: bool
    open_problems_enabled: bool


_ALLOWED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.PERIOD_SELECTED: frozenset({
        WorkflowState.PREFLIGHT_RUNNING,
    }),
    WorkflowState.PREFLIGHT_RUNNING: frozenset({
        WorkflowState.PREFLIGHT_BLOCKED,
        WorkflowState.PREFLIGHT_PASSED,
    }),
    WorkflowState.PREFLIGHT_BLOCKED: frozenset({
        WorkflowState.PREFLIGHT_RUNNING,
    }),
    WorkflowState.PREFLIGHT_PASSED: frozenset({
        WorkflowState.MATCHING_RUNNING,
        WorkflowState.PREFLIGHT_RUNNING,
    }),
    WorkflowState.MATCHING_RUNNING: frozenset({
        WorkflowState.RESOLUTION_REQUIRED,
        WorkflowState.BATCH_READY,
    }),
    WorkflowState.RESOLUTION_REQUIRED: frozenset({
        WorkflowState.BATCH_READY,
        WorkflowState.PREFLIGHT_RUNNING,
    }),
    WorkflowState.BATCH_READY: frozenset({
        WorkflowState.REVALIDATION_RUNNING,
        WorkflowState.PREFLIGHT_RUNNING,
    }),
    WorkflowState.REVALIDATION_RUNNING: frozenset({
        WorkflowState.BATCH_REVALIDATION_REQUIRED,
        WorkflowState.SENDING,
    }),
    WorkflowState.BATCH_REVALIDATION_REQUIRED: frozenset({
        WorkflowState.PREFLIGHT_RUNNING,
    }),
    WorkflowState.SENDING: frozenset({
        WorkflowState.COMPLETED,
        WorkflowState.SEND_PAUSED,
        WorkflowState.SEND_FAILED,
    }),
    WorkflowState.SEND_PAUSED: frozenset({
        WorkflowState.SENDING,
        WorkflowState.PREFLIGHT_RUNNING,
    }),
    WorkflowState.SEND_FAILED: frozenset({
        WorkflowState.PREFLIGHT_RUNNING,
    }),
    WorkflowState.COMPLETED: frozenset({
        WorkflowState.PREFLIGHT_RUNNING,
    }),
}


@dataclass(slots=True)
class WorkflowStateMachine:
    state: WorkflowState = WorkflowState.PERIOD_SELECTED
    _history: list[WorkflowState] = field(
        default_factory=lambda: [WorkflowState.PERIOD_SELECTED],
        repr=False,
    )

    @property
    def history(self) -> tuple[WorkflowState, ...]:
        return tuple(self._history)

    @property
    def can_create_telegram_client(self) -> bool:
        return self.state is WorkflowState.SENDING

    @property
    def buttons(self) -> WorkflowButtonState:
        return WorkflowButtonState(
            preflight_enabled=self.state in {
                WorkflowState.PERIOD_SELECTED,
                WorkflowState.PREFLIGHT_BLOCKED,
                WorkflowState.PREFLIGHT_PASSED,
                WorkflowState.RESOLUTION_REQUIRED,
                WorkflowState.BATCH_READY,
                WorkflowState.BATCH_REVALIDATION_REQUIRED,
                WorkflowState.SEND_PAUSED,
                WorkflowState.SEND_FAILED,
                WorkflowState.COMPLETED,
            },
            matching_enabled=self.state is WorkflowState.PREFLIGHT_PASSED,
            resolve_enabled=self.state is WorkflowState.RESOLUTION_REQUIRED,
            send_enabled=self.state is WorkflowState.BATCH_READY,
            open_problems_enabled=self.state in {
                WorkflowState.PREFLIGHT_BLOCKED,
                WorkflowState.RESOLUTION_REQUIRED,
                WorkflowState.BATCH_REVALIDATION_REQUIRED,
            },
        )

    @property
    def step_label(self) -> str:
        if self.state in {
            WorkflowState.PERIOD_SELECTED,
            WorkflowState.PREFLIGHT_RUNNING,
            WorkflowState.PREFLIGHT_BLOCKED,
            WorkflowState.PREFLIGHT_PASSED,
        }:
            return "Крок 1 із 3 — перевірка конвертації"
        if self.state in {
            WorkflowState.MATCHING_RUNNING,
            WorkflowState.RESOLUTION_REQUIRED,
            WorkflowState.BATCH_READY,
            WorkflowState.REVALIDATION_RUNNING,
            WorkflowState.BATCH_REVALIDATION_REQUIRED,
        }:
            return "Крок 2 із 3 — перевірка відповідності"
        return "Крок 3 із 3 — надсилання в Telegram"

    def transition(self, target: WorkflowState) -> None:
        allowed = _ALLOWED_TRANSITIONS[self.state]
        if target not in allowed:
            raise WorkflowTransitionError(
                f"Недопустимий перехід workflow: {self.state} → {target}"
            )
        self.state = target
        self._history.append(target)

    def start_preflight(self) -> None:
        self.transition(WorkflowState.PREFLIGHT_RUNNING)

    def finish_preflight(self, *, has_blocking_problems: bool) -> None:
        self.transition(
            WorkflowState.PREFLIGHT_BLOCKED
            if has_blocking_problems
            else WorkflowState.PREFLIGHT_PASSED
        )

    def start_matching(self) -> None:
        self.transition(WorkflowState.MATCHING_RUNNING)

    def finish_matching(self, *, has_unresolved_problems: bool) -> None:
        self.transition(
            WorkflowState.RESOLUTION_REQUIRED
            if has_unresolved_problems
            else WorkflowState.BATCH_READY
        )

    def finish_resolution(self, *, unresolved_count: int) -> None:
        if self.state is not WorkflowState.RESOLUTION_REQUIRED:
            raise WorkflowTransitionError(
                "Рішення можна завершити лише зі стану RESOLUTION_REQUIRED"
            )
        if unresolved_count < 0:
            raise ValueError("Unresolved count cannot be negative")
        if unresolved_count == 0:
            self.transition(WorkflowState.BATCH_READY)

    def start_revalidation(self) -> None:
        self.transition(WorkflowState.REVALIDATION_RUNNING)

    def finish_revalidation(self, *, success: bool) -> None:
        self.transition(
            WorkflowState.SENDING
            if success
            else WorkflowState.BATCH_REVALIDATION_REQUIRED
        )

    def finish_sending(
        self,
        *,
        success: bool,
        paused: bool = False,
    ) -> None:
        self.transition(
            WorkflowState.COMPLETED
            if success
            else (
                WorkflowState.SEND_PAUSED
                if paused
                else WorkflowState.SEND_FAILED
            )
        )
