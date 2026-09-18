"""Unit tests for the structural-invariant domain (``invariants.py``).

Everything here is pure: no Streamlit, no SQL, no network and no real ``.env``.
"""

import unittest

from invariants import (
    CHECK_NONE,
    CHECK_NO_BULK_RESET,
    CHECK_NO_VALIDATION_BYPASS,
    ENFORCEMENT_ADVISORY,
    ENFORCEMENT_HARD,
    EVENT_VALIDATION_PASSED,
    Invariant,
    InvariantConflictError,
    KIND_DATA,
    KIND_GUARD,
    KIND_POLICY,
    PHASE_ACTION,
    PHASE_COMMIT,
    PHASE_REQUEST,
    SCOPE_GLOBAL,
    SCOPE_TASK,
    SOURCE_SEED,
    evaluate_check,
    find_action_conflict,
    find_request_conflict,
    find_transition_conflict,
    format_conflict_message,
    format_structural_invariants_block,
    normalize_invariant,
    select_applicable,
    validate_code,
)
from tasks import ACTION_RUN_STEP, ACTION_RUN_VALIDATION, EVENT_PAUSE


def invariant(**overrides):
    """A valid hard global rule, overridable per test."""
    fields = dict(
        id=1,
        code="INV-TEST",
        title="Test rule",
        text="Do not do the forbidden thing.",
        scope=SCOPE_GLOBAL,
        enforcement=ENFORCEMENT_HARD,
        kind=KIND_GUARD,
        triggers=("forbidden thing",),
        alternative="Do the allowed thing.",
        source=SOURCE_SEED,
    )
    fields.update(overrides)
    return Invariant(**fields)


class ValidateCodeTest(unittest.TestCase):
    def test_accepts_supported_codes(self):
        for code in ("INV-1", "inv.test_1", "A", "a.b-c_d"):
            with self.subTest(code=code):
                self.assertEqual(validate_code(code), code)

    def test_rejects_empty_and_bad_codes(self):
        for code in ("", "   ", "-leading", ".dot", "space code", "a b"):
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    validate_code(code)


class NormalizeInvariantTest(unittest.TestCase):
    def test_normalizes_and_strips(self):
        normalized = normalize_invariant(
            invariant(
                code="  INV-TEST  ",
                title="  Title  ",
                text="  Text  ",
                triggers=(" a ", "", "b"),
                alternative="  alt  ",
            )
        )
        self.assertEqual(normalized.code, "INV-TEST")
        self.assertEqual(normalized.title, "Title")
        self.assertEqual(normalized.text, "Text")
        self.assertEqual(normalized.triggers, ("a", "b"))
        self.assertEqual(normalized.alternative, "alt")

    def test_rejects_empty_title_or_text(self):
        with self.assertRaises(ValueError):
            normalize_invariant(invariant(title="   "))
        with self.assertRaises(ValueError):
            normalize_invariant(invariant(text=""))

    def test_rejects_unknown_enum_values(self):
        for overrides in (
            {"scope": "team"},
            {"enforcement": "soft"},
            {"kind": "other"},
            {"check_kind": "unknown_check"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    normalize_invariant(invariant(**overrides))

    def test_task_scope_requires_a_task_id_and_global_forbids_it(self):
        with self.assertRaises(ValueError):
            normalize_invariant(invariant(scope=SCOPE_TASK))
        with self.assertRaises(ValueError):
            normalize_invariant(invariant(scope=SCOPE_GLOBAL, task_id=7))
        normalized = normalize_invariant(invariant(scope=SCOPE_TASK, task_id=7))
        self.assertEqual(normalized.task_id, 7)

    def test_advisory_cannot_declare_a_check_or_guards(self):
        for overrides in (
            {"check_kind": CHECK_NO_VALIDATION_BYPASS},
            {"guard_actions": (ACTION_RUN_STEP,)},
            {"guard_events": (EVENT_PAUSE,)},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    normalize_invariant(
                        invariant(enforcement=ENFORCEMENT_ADVISORY, **overrides)
                    )

    def test_hard_requires_at_least_one_surface(self):
        with self.assertRaises(ValueError):
            normalize_invariant(
                invariant(
                    triggers=(),
                    guard_actions=(),
                    guard_events=(),
                )
            )


class SelectApplicableTest(unittest.TestCase):
    def test_only_active_rules_are_selected(self):
        rules = [invariant(id=1, code="A"), invariant(id=2, code="B", is_active=False)]
        selected = select_applicable(rules, task_id=5)
        self.assertEqual([rule.code for rule in selected], ["A"])

    def test_global_applies_and_task_rule_matches_its_task_only(self):
        rules = [
            invariant(id=1, code="GLOBAL", scope=SCOPE_GLOBAL),
            invariant(id=2, code="TASK-1", scope=SCOPE_TASK, task_id=1),
            invariant(id=3, code="TASK-2", scope=SCOPE_TASK, task_id=2),
        ]
        selected = select_applicable(rules, task_id=1)
        self.assertEqual([rule.code for rule in selected], ["GLOBAL", "TASK-1"])
        self.assertEqual(
            [rule.code for rule in select_applicable(rules, task_id=99)], ["GLOBAL"]
        )
        self.assertEqual(
            [rule.code for rule in select_applicable(rules, task_id=None)], ["GLOBAL"]
        )

    def test_order_is_global_task_then_hard_advisory_then_code(self):
        rules = [
            invariant(id=4, code="Z-ADV", enforcement=ENFORCEMENT_ADVISORY, triggers=()),
            invariant(id=3, code="T-ADV", scope=SCOPE_TASK, task_id=1,
                      enforcement=ENFORCEMENT_ADVISORY, triggers=()),
            invariant(id=2, code="G-HARD", scope=SCOPE_GLOBAL,
                      enforcement=ENFORCEMENT_HARD),
            invariant(id=1, code="T-HARD", scope=SCOPE_TASK, task_id=1,
                      enforcement=ENFORCEMENT_HARD),
        ]
        selected = select_applicable(rules, task_id=1)
        self.assertEqual(
            [rule.code for rule in selected],
            ["G-HARD", "Z-ADV", "T-HARD", "T-ADV"],
        )

    def test_global_rule_applies_to_a_new_task(self):
        rule = invariant(scope=SCOPE_GLOBAL)
        self.assertEqual(select_applicable([rule], task_id=123), [rule])


class FormatBlockTest(unittest.TestCase):
    def test_empty_or_inactive_yields_none(self):
        self.assertIsNone(format_structural_invariants_block([]))
        self.assertIsNone(
            format_structural_invariants_block([invariant(is_active=False)])
        )

    def test_block_lists_code_enforcement_scope_and_alternative(self):
        hard = invariant(id=1, code="INV-HARD", enforcement=ENFORCEMENT_HARD)
        advisory = invariant(
            id=2,
            code="INV-ADV",
            enforcement=ENFORCEMENT_ADVISORY,
            check_kind=CHECK_NONE,
            guard_actions=(),
            guard_events=(),
            triggers=(),
            alternative="",
        )
        block = format_structural_invariants_block([hard, advisory])
        self.assertIn("INV-HARD", block)
        self.assertIn("hard, global", block)
        self.assertIn("INV-ADV", block)
        self.assertIn("advisory", block)
        self.assertIn("Do the allowed thing.", block)

    def test_task_scope_is_rendered_with_its_id(self):
        rule = invariant(scope=SCOPE_TASK, task_id=7, title="Scoped")
        block = format_structural_invariants_block([rule])
        self.assertIn("task:7", block)


class TriggerTest(unittest.TestCase):
    def test_request_trigger_matches_case_insensitively(self):
        rule = invariant(triggers=("skip validation",))
        conflict = find_request_conflict([rule], "Please SKIP VALIDATION now")
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict.phase, PHASE_REQUEST)
        self.assertEqual(conflict.trigger, "skip validation")

    def test_request_trigger_negative(self):
        rule = invariant(triggers=("skip validation",))
        self.assertIsNone(find_request_conflict([rule], "run validation now"))
        self.assertIsNone(find_request_conflict([rule], "   "))

    def test_advisory_never_blocks_a_request(self):
        rule = invariant(
            enforcement=ENFORCEMENT_ADVISORY,
            triggers=("skip validation",),
            guard_actions=(),
            guard_events=(),
        )
        self.assertIsNone(find_request_conflict([rule], "skip validation"))

    def test_inactive_hard_rule_does_not_block(self):
        rule = invariant(triggers=("skip validation",), is_active=False)
        self.assertIsNone(find_request_conflict([rule], "skip validation"))


class PredicateTest(unittest.TestCase):
    def test_action_guard_blocks_the_declared_action(self):
        rule = invariant(guard_actions=(ACTION_RUN_STEP,), triggers=())
        conflict = find_action_conflict([rule], ACTION_RUN_STEP)
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict.phase, PHASE_ACTION)
        self.assertIsNone(find_action_conflict([rule], ACTION_RUN_VALIDATION))
        self.assertIsNone(find_action_conflict([rule], None))

    def test_transition_guard_event_blocks_the_event(self):
        rule = invariant(guard_events=(EVENT_PAUSE,), triggers=())
        conflict = find_transition_conflict([rule], EVENT_PAUSE)
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict.phase, PHASE_COMMIT)
        self.assertIsNone(find_transition_conflict([rule], EVENT_VALIDATION_PASSED))

    def test_no_validation_bypass_check(self):
        rule = invariant(
            check_kind=CHECK_NO_VALIDATION_BYPASS,
            guard_events=(EVENT_VALIDATION_PASSED,),
            triggers=(),
        )
        blocked = find_transition_conflict(
            [rule],
            EVENT_VALIDATION_PASSED,
            context={"action": ACTION_RUN_STEP},
        )
        self.assertIsNotNone(blocked)
        # The real validation runs through ACTION_RUN_VALIDATION and passes.
        self.assertIsNone(
            find_transition_conflict(
                [rule],
                EVENT_VALIDATION_PASSED,
                context={"action": ACTION_RUN_VALIDATION},
            )
        )
        # Other transitions are not affected.
        self.assertIsNone(
            find_transition_conflict([rule], EVENT_PAUSE, context={})
        )

    def test_no_bulk_reset_check(self):
        rule = invariant(
            check_kind=CHECK_NO_BULK_RESET, kind=KIND_DATA, triggers=()
        )
        self.assertIsNone(
            find_transition_conflict([rule], EVENT_PAUSE, context={})
        )
        self.assertIsNotNone(
            evaluate_check(CHECK_NO_BULK_RESET, {"operation": "reset_all"})
        )
        self.assertIsNone(
            evaluate_check(CHECK_NO_BULK_RESET, {"operation": "delete_one_chat"})
        )

    def test_unknown_check_passes(self):
        self.assertIsNone(evaluate_check("", {}))
        self.assertIsNone(evaluate_check("unknown", {}))


class ConflictMessageTest(unittest.TestCase):
    def test_message_names_the_rule_version_scope_and_path(self):
        conflict = find_request_conflict(
            [invariant(code="INV-X", version=3, triggers=("forbidden",))], "forbidden"
        )
        message = format_conflict_message(conflict)
        self.assertIn("INV-X", message)
        self.assertIn("v3", message)
        self.assertIn("global", message)
        self.assertIn("hard", message)
        self.assertIn("Nothing was done", message)
        self.assertIn("Allowed alternative", message)
        self.assertIn("Diagnostics / Task", message)

    def test_error_wraps_the_conflict(self):
        conflict = find_request_conflict([invariant(triggers=("bad",))], "bad")
        error = InvariantConflictError(conflict)
        self.assertIs(error.conflict, conflict)
        self.assertIn("INV-TEST", str(error))


if __name__ == "__main__":
    unittest.main()
