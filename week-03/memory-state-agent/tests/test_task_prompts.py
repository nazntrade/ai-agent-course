"""Unit tests for the step-facts guard of ``task_prompts``.

Pure: no I/O, no provider call. They check the conservative detector on the
reported defect shape (invented ``EVT-...`` ids, an unknown numeric event id, a
wrong stage/status claim, the denial of a recorded event) and on legitimate
text (domain vocabulary, guard frames, real event ids), plus the corrective
feedback and the execution prompt rules.
"""

import unittest
from dataclasses import replace

from task_prompts import (
    REASON_CONTRADICTS_FACTS,
    REASON_FABRICATED_REFERENCE,
    REASON_UNCONFIRMED_ATTEMPT,
    TASK_EXECUTION_SYSTEM_PROMPT,
    StepEventFact,
    StepFacts,
    StepFactsViolationError,
    StepRefusalFact,
    build_step_messages,
    step_retry_feedback,
    validate_step_text,
)

FACTS = StepFacts(
    stage="execution",
    status="active",
    current_step="Step two",
    current_step_index=2,
    expected_action_type="run_step",
    expected_action_text="Run the current step",
    version=4,
    events=(
        StepEventFact(
            id=1, event_type="TASK_CREATED", created_at="2026-09-20 10:00:00"
        ),
        StepEventFact(
            id=2, event_type="PLAN_CREATED", created_at="2026-09-20 10:01:00"
        ),
        StepEventFact(
            id=3, event_type="PLAN_ACCEPTED", created_at="2026-09-20 10:02:00"
        ),
        StepEventFact(
            id=4, event_type="STEP_COMPLETED", created_at="2026-09-20 10:03:00"
        ),
    ),
    refusals=(
        StepRefusalFact(
            id=1,
            action="run_validation",
            reason="expected_action_mismatch",
            created_at="2026-09-20 10:04:00",
        ),
    ),
)

# The reported defect shape: the model invented journal ids and claimed a state
# that contradicts the stored Event timeline.
REPORTED_DIRTY_TEXT = (
    "Шаг 2 выполнен: создано событие EVT-EXEC-001. "
    "Провести переход в execution ещё не выполнялся. "
    "Стадия: planning. Статус: completed. "
    "plan_accepted отсутствует в журнале."
)

# The Day 15 fix defect: no refusal is stored, yet the step text claims an
# attempted (and rejected) transition.
REPORTED_UNCONFIRMED_TEXT = (
    "Шаг 3: Инициирован недопустимый переход из стадии execution "
    "к завершению выполнения; попытка перехода была отклонена."
)

# The same snapshot with an empty refusal audit.
NO_REFUSAL_FACTS = replace(FACTS, refusals=())

# A snapshot whose audit confirms a refused ``finish_execution``.
CONFIRMED_REFUSAL_FACTS = replace(
    FACTS,
    refusals=(
        StepRefusalFact(
            id=7,
            action="finish_execution",
            reason="progress_incomplete",
            created_at="2026-09-20 10:05:00",
        ),
    ),
)


class ValidateStepTextTest(unittest.TestCase):
    def assert_violation(self, text, facts=FACTS):
        with self.assertRaises(StepFactsViolationError) as caught:
            validate_step_text(text, facts)
        return caught.exception

    def test_invented_evt_id_is_rejected(self):
        exc = self.assert_violation("Создано событие EVT-EXEC-001.")
        self.assertEqual(
            exc.violations[0].reason, REASON_FABRICATED_REFERENCE
        )
        self.assertTrue(exc.violations[0].fragment.startswith("EVT"))

    def test_unknown_numeric_event_reference_is_rejected(self):
        exc = self.assert_violation("Событие 4242 записано в журнал.")
        self.assertEqual(
            exc.violations[0].reason, REASON_FABRICATED_REFERENCE
        )

    def test_known_numeric_event_reference_is_allowed(self):
        validate_step_text("Событие 3 зафиксировано в журнале.", FACTS)

    def test_wrong_stage_or_status_is_rejected(self):
        exc = self.assert_violation("Стадия: planning. Статус: completed.")
        reasons = {violation.reason for violation in exc.violations}
        self.assertEqual(reasons, {REASON_CONTRADICTS_FACTS})
        self.assertGreaterEqual(len(exc.violations), 2)

    def test_matching_stage_and_status_are_allowed(self):
        validate_step_text("stage: execution, status: active.", FACTS)

    def test_done_claim_while_active_is_rejected(self):
        exc = self.assert_violation("Проверка завершена: задача выполнена.")
        self.assertIn(
            REASON_CONTRADICTS_FACTS,
            {violation.reason for violation in exc.violations},
        )

    def test_denial_of_a_recorded_event_is_rejected(self):
        exc = self.assert_violation("Событие plan_accepted не наступило.")
        self.assertEqual(
            exc.violations[0].reason, REASON_CONTRADICTS_FACTS
        )

    def test_denial_of_the_current_transition_is_rejected(self):
        exc = self.assert_violation("Переход в execution ещё не выполнялся.")
        self.assertEqual(
            exc.violations[0].reason, REASON_CONTRADICTS_FACTS
        )

    def test_guard_frame_is_not_a_contradiction(self):
        validate_step_text(
            "Проверить через Transition guard, что возврат в planning "
            "запрещён и записан в аудит отказов.",
            FACTS,
        )

    # --- Unconfirmed attempt guard (Day 15 fix) ---------------------------

    def test_unconfirmed_attempt_without_a_stored_refusal_is_rejected(self):
        exc = self.assert_violation(REPORTED_UNCONFIRMED_TEXT, NO_REFUSAL_FACTS)
        reasons = {violation.reason for violation in exc.violations}
        self.assertIn(REASON_UNCONFIRMED_ATTEMPT, reasons)

    def test_attempt_claim_matching_a_stored_refusal_is_allowed(self):
        validate_step_text(
            "Переход finish_execution был отклонён с причиной "
            "progress_incomplete.",
            CONFIRMED_REFUSAL_FACTS,
        )

    def test_attempt_claim_naming_a_missing_refusal_is_rejected(self):
        exc = self.assert_violation(
            "Попытка finish_execution выполнена и записана в аудит."
        )
        reasons = {violation.reason for violation in exc.violations}
        self.assertIn(REASON_UNCONFIRMED_ATTEMPT, reasons)

    def test_conditional_guard_form_is_allowed_without_refusals(self):
        validate_step_text(
            "Transition guard показывает, что Finish execution сейчас был бы "
            "отклонён с причиной progress_incomplete.",
            NO_REFUSAL_FACTS,
        )

    # --- Negation and stage matching (Day 15 post-review) ----------------

    def test_negated_performed_transition_is_not_a_claim(self):
        # "не выполнен" must not classify as a performed claim: the substring
        # "выполнен" lives inside the denial.
        validate_step_text("Переход в validation не выполнен.", FACTS)

    def test_performed_transition_matching_one_named_stage_is_allowed(self):
        # Both stages are named and execution is the current one: no violation.
        validate_step_text("Переход из planning в execution выполнен.", FACTS)

    def test_performed_transition_with_only_another_stage_is_rejected(self):
        exc = self.assert_violation("Переход в validation выполнен.")
        reasons = {violation.reason for violation in exc.violations}
        self.assertIn(REASON_UNCONFIRMED_ATTEMPT, reasons)

    def test_negated_attempt_markers_are_not_claims(self):
        validate_step_text("Переход не инициирован.", FACTS)
        validate_step_text("Переход не отклонён.", FACTS)
        validate_step_text("Переход не заблокирован.", FACTS)

    def test_negated_recorded_marker_is_not_a_claim(self):
        validate_step_text("Переход не зафиксирован в аудите.", FACTS)

    def test_negated_attempt_denials_are_not_claims_with_any_snapshot(self):
        # The true denial must not be misread through a later marker (the bare
        # "аудит" once sat outside the negation window and flagged these).
        for facts in (NO_REFUSAL_FACTS, FACTS):
            for text in (
                "Отказ не зафиксирован.",
                "Попытка не зафиксирована в аудите.",
                "Запись об отказе не внесена.",
            ):
                with self.subTest(facts_refusals=len(facts.refusals), text=text):
                    validate_step_text(text, facts)

    def test_positive_attempt_claims_without_a_record_are_rejected(self):
        for text in (
            "Попытка перехода зафиксирована в аудите.",
            "Отказ зафиксирован.",
            "Запись об отказе внесена.",
        ):
            with self.subTest(text=text):
                exc = self.assert_violation(text, NO_REFUSAL_FACTS)
                reasons = {violation.reason for violation in exc.violations}
                self.assertIn(REASON_UNCONFIRMED_ATTEMPT, reasons)

    def test_positive_performed_stems_are_detected(self):
        for text in (
            "Переход в validation выполнялся.",
            "Переход в validation завершён.",
            "Переход в validation совершен.",
        ):
            with self.subTest(text=text):
                exc = self.assert_violation(text)
                reasons = {violation.reason for violation in exc.violations}
                self.assertIn(REASON_UNCONFIRMED_ATTEMPT, reasons)

    def test_negated_performed_stems_are_not_claims(self):
        validate_step_text("Переход в validation не выполнялся.", FACTS)
        validate_step_text("Переход в validation не завершён.", FACTS)
        validate_step_text("Переход в validation не совершен.", FACTS)

    def test_unconfirmed_performed_attempt_forms_are_rejected(self):
        # "Попытка перехода + performed participle" must take the attempt branch
        # even when no stage is named; an empty audit cannot confirm it.
        for text in (
            "Попытка перехода совершена.",
            "Попытка перехода состоялась.",
            "Попытка перехода осуществлена.",
            "Попытка перехода произведена.",
        ):
            with self.subTest(text=text):
                exc = self.assert_violation(text, NO_REFUSAL_FACTS)
                reasons = {violation.reason for violation in exc.violations}
                self.assertIn(REASON_UNCONFIRMED_ATTEMPT, reasons)

    def test_negated_performed_attempt_forms_are_not_claims(self):
        # The negation before the marker makes each form a true denial, not an
        # unconfirmed attempt claim, even with an empty audit.
        for text in (
            "Попытка перехода не совершена.",
            "Попытка перехода не состоялась.",
            "Попытка перехода не осуществлена.",
            "Попытка перехода не произведена.",
        ):
            with self.subTest(text=text):
                validate_step_text(text, NO_REFUSAL_FACTS)

    def test_state_noun_is_not_read_as_the_performed_stem(self):
        # The state stem is "состоял" (состоялась/состоялся), so the noun
        # "состояние" (and its cases) must not be misread as an attempt claim:
        # truthful step text about the task state was rejected with an empty
        # audit before the fix.
        for text in (
            "Попытка изменения состояния не предпринималась.",
            "Попытка проверить состояние задачи.",
        ):
            with self.subTest(text=text):
                validate_step_text(text, NO_REFUSAL_FACTS)

    def test_performed_state_attempt_forms_stay_blocked_without_refusals(self):
        # The shortened state stem must keep matching the real performed forms.
        for text in (
            "Попытка перехода состоялась.",
            "Попытка перехода состоялся.",
            "Попытка перехода состоялось.",
            "Попытка перехода состояли.",
        ):
            with self.subTest(text=text):
                exc = self.assert_violation(text, NO_REFUSAL_FACTS)
                reasons = {violation.reason for violation in exc.violations}
                self.assertIn(REASON_UNCONFIRMED_ATTEMPT, reasons)

    def test_previously_blocked_attempt_and_transition_forms_remain_blocked(self):
        exc = self.assert_violation("Попытка перехода выполнена.", NO_REFUSAL_FACTS)
        reasons = {violation.reason for violation in exc.violations}
        self.assertIn(REASON_UNCONFIRMED_ATTEMPT, reasons)

        exc = self.assert_violation("Переход в validation выполнен.")
        reasons = {violation.reason for violation in exc.violations}
        self.assertIn(REASON_UNCONFIRMED_ATTEMPT, reasons)

        validate_step_text("Переход из planning в execution выполнен.", FACTS)

    def test_conditional_match_respects_word_boundaries(self):
        # "поможет" contains "может"; a substring check would misread the whole
        # sentence as a prediction and silently skip the performed claim.
        exc = self.assert_violation(
            "Инструмент поможет, переход в validation выполнен.",
            NO_REFUSAL_FACTS,
        )
        reasons = {violation.reason for violation in exc.violations}
        self.assertIn(REASON_UNCONFIRMED_ATTEMPT, reasons)

    def test_audit_date_is_not_read_as_an_audit_id(self):
        # "аудит от 2026-09-20" is a date, not an audit reference.
        validate_step_text("Проверка завершена, аудит от 2026-09-20.", FACTS)

    def test_audit_reference_without_hash_is_still_checked(self):
        exc = self.assert_violation("Отказ зафиксирован, аудит 4242.")
        reasons = {violation.reason for violation in exc.violations}
        self.assertIn(REASON_FABRICATED_REFERENCE, reasons)

    def test_unknown_audit_reference_is_rejected(self):
        with self.assertRaises(StepFactsViolationError) as caught:
            validate_step_text("Отказ зафиксирован, audit #4242.", FACTS)
        self.assertEqual(
            caught.exception.violations[0].reason, REASON_FABRICATED_REFERENCE
        )

    def test_audit_reference_forms_are_recognized(self):
        # The numeric reference must be read after every supported connector.
        for text in (
            "Проверка завершена, audit #12.",
            "Проверка завершена, audit id 12.",
            "Проверка завершена, audit 12.",
            "Проверка завершена, аудит №12.",
            "Проверка завершена, аудит: 12.",
        ):
            with self.subTest(text=text):
                exc = self.assert_violation(text)
                reasons = {violation.reason for violation in exc.violations}
                self.assertEqual(reasons, {REASON_FABRICATED_REFERENCE})

    def test_known_audit_reference_is_allowed(self):
        validate_step_text(
            "Отказ run_validation зафиксирован: audit #1.", FACTS
        )

    def test_dirty_reported_text_keeps_the_existing_reasons(self):
        exc = self.assert_violation(REPORTED_DIRTY_TEXT)
        reasons = {violation.reason for violation in exc.violations}
        self.assertEqual(
            reasons, {REASON_FABRICATED_REFERENCE, REASON_CONTRADICTS_FACTS}
        )

    def test_clean_domain_text_is_allowed(self):
        validate_step_text(
            "Шаг 2 выполнен: результат — перечень доступных действий. "
            "Событие PLAN_ACCEPTED зафиксировано в журнале, переход в "
            "execution состоялся.",
            FACTS,
        )

    def test_none_facts_skips_the_check(self):
        validate_step_text(REPORTED_DIRTY_TEXT, None)

    def test_reported_text_is_rejected_with_compact_error(self):
        exc = self.assert_violation(REPORTED_DIRTY_TEXT)
        reasons = {violation.reason for violation in exc.violations}
        self.assertIn(REASON_FABRICATED_REFERENCE, reasons)
        self.assertIn(REASON_CONTRADICTS_FACTS, reasons)
        self.assertLessEqual(len(str(exc)), 200)

    def test_retry_feedback_is_one_russian_user_message(self):
        exc = self.assert_violation(REPORTED_DIRTY_TEXT)
        feedback = step_retry_feedback(exc)
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0]["role"], "user")
        content = feedback[0]["content"]
        self.assertIn("task_facts", content)
        self.assertIn("не выдумывай", content)
        self.assertIn(REASON_FABRICATED_REFERENCE, content)

    def test_execution_prompt_and_action_message_carry_the_rules(self):
        self.assertIn("task_facts", TASK_EXECUTION_SYSTEM_PROMPT)
        self.assertIn("выдумывай", TASK_EXECUTION_SYSTEM_PROMPT)
        messages = build_step_messages(
            {"index": 1, "title": "Step", "description": "Do it"}
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("task_facts", messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
