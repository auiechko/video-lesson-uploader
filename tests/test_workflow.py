from __future__ import annotations

import unittest

from lesson_video_uploader.workflow import (
    WorkflowState,
    WorkflowStateMachine,
    WorkflowTransitionError,
)


class WorkflowStateMachineTests(unittest.TestCase):
    def test_unrendered_recording_blocks_matching_and_send(self) -> None:
        workflow = WorkflowStateMachine()

        workflow.start_preflight()
        workflow.finish_preflight(has_blocking_problems=True)

        self.assertEqual(workflow.state, WorkflowState.PREFLIGHT_BLOCKED)
        self.assertTrue(workflow.buttons.preflight_enabled)
        self.assertFalse(workflow.buttons.matching_enabled)
        self.assertFalse(workflow.buttons.send_enabled)

    def test_preflight_pass_enables_matching_but_not_send(self) -> None:
        workflow = WorkflowStateMachine()

        workflow.start_preflight()
        workflow.finish_preflight(has_blocking_problems=False)

        self.assertEqual(workflow.state, WorkflowState.PREFLIGHT_PASSED)
        self.assertTrue(workflow.buttons.matching_enabled)
        self.assertFalse(workflow.buttons.send_enabled)

    def test_unresolved_matching_conflict_blocks_send(self) -> None:
        workflow = WorkflowStateMachine()
        workflow.start_preflight()
        workflow.finish_preflight(has_blocking_problems=False)

        workflow.start_matching()
        workflow.finish_matching(has_unresolved_problems=True)

        self.assertEqual(workflow.state, WorkflowState.RESOLUTION_REQUIRED)
        self.assertTrue(workflow.buttons.resolve_enabled)
        self.assertFalse(workflow.buttons.send_enabled)

    def test_resolving_every_problem_makes_batch_ready(self) -> None:
        workflow = WorkflowStateMachine()
        workflow.start_preflight()
        workflow.finish_preflight(has_blocking_problems=False)
        workflow.start_matching()
        workflow.finish_matching(has_unresolved_problems=True)

        workflow.finish_resolution(unresolved_count=0)

        self.assertEqual(workflow.state, WorkflowState.BATCH_READY)
        self.assertTrue(workflow.buttons.send_enabled)

    def test_direct_send_is_rejected_before_revalidation(self) -> None:
        workflow = WorkflowStateMachine()

        with self.assertRaises(WorkflowTransitionError):
            workflow.transition(WorkflowState.SENDING)

        self.assertFalse(workflow.can_create_telegram_client)

    def test_revalidation_failure_returns_to_blocked_state(self) -> None:
        workflow = self._ready_workflow()

        workflow.start_revalidation()
        workflow.finish_revalidation(success=False)

        self.assertEqual(
            workflow.state,
            WorkflowState.BATCH_REVALIDATION_REQUIRED,
        )
        self.assertFalse(workflow.buttons.send_enabled)

    def test_successful_full_workflow_has_strict_transition_history(self) -> None:
        workflow = WorkflowStateMachine()
        workflow.start_preflight()
        workflow.finish_preflight(has_blocking_problems=False)
        workflow.start_matching()
        workflow.finish_matching(has_unresolved_problems=False)
        workflow.start_revalidation()
        workflow.finish_revalidation(success=True)
        workflow.finish_sending(success=True)

        self.assertEqual(workflow.state, WorkflowState.COMPLETED)
        self.assertEqual(
            workflow.history,
            (
                WorkflowState.PERIOD_SELECTED,
                WorkflowState.PREFLIGHT_RUNNING,
                WorkflowState.PREFLIGHT_PASSED,
                WorkflowState.MATCHING_RUNNING,
                WorkflowState.BATCH_READY,
                WorkflowState.REVALIDATION_RUNNING,
                WorkflowState.SENDING,
                WorkflowState.COMPLETED,
            ),
        )

    @staticmethod
    def _ready_workflow() -> WorkflowStateMachine:
        workflow = WorkflowStateMachine()
        workflow.start_preflight()
        workflow.finish_preflight(has_blocking_problems=False)
        workflow.start_matching()
        workflow.finish_matching(has_unresolved_problems=False)
        return workflow


if __name__ == "__main__":
    unittest.main()
