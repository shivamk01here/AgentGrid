"""Tests for typed identifiers.

The derivation tests matter most: an idempotency key that is not stable
across processes and releases silently stops preventing double payments.
"""

import pytest

from ledgerloop.core.ids import (
    ActionId,
    ApprovalId,
    CorrelationId,
    EntryId,
    IdempotencyKey,
    RunId,
    StepId,
    TenantId,
)

ALL_TYPES = (TenantId, RunId, StepId, ActionId, ApprovalId, EntryId, CorrelationId)


class TestIdentifier:
    @pytest.mark.parametrize("cls", ALL_TYPES)
    def test_generate_carries_the_type_prefix(self, cls):
        assert cls.generate().value.startswith(f"{cls.prefix}_")

    @pytest.mark.parametrize("cls", ALL_TYPES)
    def test_generated_ids_are_unique(self, cls):
        assert len({cls.generate().value for _ in range(200)}) == 200

    def test_empty_value_rejected(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            RunId("")

    def test_illegal_characters_rejected(self):
        with pytest.raises(ValueError, match="1-128 characters"):
            RunId("run with spaces")

    def test_overlong_value_rejected(self):
        with pytest.raises(ValueError):
            RunId("r" * 129)

    def test_external_ids_without_our_prefix_are_accepted(self):
        # A provider's own reference must survive being carried, unmangled.
        assert RunId("psp-ref:88213").value == "psp-ref:88213"

    def test_str_returns_the_bare_value(self):
        run_id = RunId.generate()
        assert str(run_id) == run_id.value

    def test_identifiers_are_hashable_and_comparable(self):
        a = RunId("run_aaa")
        assert a == RunId("run_aaa")
        assert len({a, RunId("run_aaa")}) == 1
        assert RunId("run_a") < RunId("run_b")

    def test_distinct_types_do_not_compare_equal(self):
        # A StepId reaching a run lookup should be a bug you can see.
        assert RunId("x_1") != StepId("x_1")

    def test_parse_round_trips(self):
        original = RunId.generate()
        assert RunId.parse(original.value) == original


class TestIdempotencyKey:
    def test_derivation_is_deterministic(self):
        first = IdempotencyKey.derive("refund", "ord_1", "125050")
        second = IdempotencyKey.derive("refund", "ord_1", "125050")
        assert first == second

    def test_different_content_yields_different_keys(self):
        assert IdempotencyKey.derive("refund", "ord_1", "100") != IdempotencyKey.derive(
            "refund", "ord_1", "200"
        )

    def test_part_order_matters(self):
        assert IdempotencyKey.derive("a", "b") != IdempotencyKey.derive("b", "a")

    def test_parts_cannot_be_split_ambiguously(self):
        # Concatenation would collide these; the separator must prevent it.
        assert IdempotencyKey.derive("ab", "c") != IdempotencyKey.derive("a", "bc")

    def test_derived_key_carries_the_prefix(self):
        assert IdempotencyKey.derive("refund", "ord_1").value.startswith("idk_")

    def test_derived_key_does_not_leak_its_inputs(self):
        # Parts can contain PII and end up in a provider's logs.
        key = IdempotencyKey.derive("refund", "customer@example.com")
        assert "customer@example.com" not in key.value

    def test_no_parts_rejected(self):
        with pytest.raises(ValueError, match="at least one part"):
            IdempotencyKey.derive()

    def test_empty_part_rejected(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            IdempotencyKey.derive("refund", "")

    def test_whitespace_only_part_rejected(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            IdempotencyKey.derive("refund", "   ")
