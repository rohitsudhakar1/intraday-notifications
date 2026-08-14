"""Rule validation.

The premise: a rule that can never fire is worse than no rule at all, because
the user believes they are covered. Every case below is a rule that would look
saved, sit in the list, and never do anything.
"""

import pytest
from pydantic import ValidationError

from app import catalog
from app.models import RuleCreate

BASE = {
    "name": "test",
    "subject_type": "queue",
    "metric": "tickets_waiting",
    "operator": "gt",
    "threshold": 20.0,
    "recipient_id": "u_lead",
}


def build(**overrides) -> RuleCreate:
    return RuleCreate(**{**BASE, **overrides})


def test_a_valid_rule_is_accepted():
    assert build().metric == "tickets_waiting"


def test_an_unknown_metric_is_rejected_with_the_valid_options():
    with pytest.raises(ValidationError) as error:
        build(metric="tickets_pending")
    message = str(error.value)
    assert "unknown metric" in message
    assert "tickets_waiting" in message, "the error should show the user what is valid"


def test_a_queue_metric_on_an_agent_rule_is_rejected():
    """This rule would sit in the list looking healthy and never match anything,
    because agent events carry no ticket counts."""
    with pytest.raises(ValidationError, match="describes a queue"):
        build(subject_type="agent")


def test_the_percent_mistake_is_caught_and_explained():
    """sla_ratio is a fraction: 1.0 is 100%. A user typing 80 means 80% and
    gets a rule that can never fire. Catching it is easy; explaining it in the
    error is what makes the difference."""
    with pytest.raises(ValidationError, match=r"Did you mean 0\.8"):
        build(metric="sla_ratio", threshold=80.0)


def test_a_ratio_below_one_is_fine():
    assert build(metric="sla_ratio", threshold=0.8).threshold == 0.8


def test_a_scope_with_no_ids_is_rejected():
    with pytest.raises(ValidationError, match="requires at least one id"):
        build(scope_type="ids", scope_ids=[])


def test_scope_all_with_ids_is_rejected():
    """Ambiguous rather than harmless: the ids look like they narrow the rule,
    and they do not."""
    with pytest.raises(ValidationError, match="must not list ids"):
        build(scope_type="all", scope_ids=["billing"])


def test_queue_scope_is_only_meaningful_for_agent_rules():
    with pytest.raises(ValidationError, match="only applies to agent rules"):
        build(scope_type="queues", scope_ids=["billing"])


def test_an_agent_rule_scoped_by_queue_is_accepted():
    """The "any of my agents" case: agents serving these queues."""
    rule = build(subject_type="agent", metric="time_on_call", threshold=2700.0,
                 scope_type="queues", scope_ids=["billing"])
    assert rule.scope_ids == ["billing"]


def test_a_negative_duration_is_rejected():
    with pytest.raises(ValidationError):
        build(duration_sec=-60)


# ------------------------------------------------------------------- catalog


def test_every_metric_renders_a_readable_summary():
    """The summary is what the user reads back to check they built what they
    meant, so it must never fall through to raw field names."""
    for metric in catalog.METRICS.values():
        summary = catalog.describe(metric.key, "gt", 100.0, 600)
        assert metric.label in summary
        assert "for 10m" in summary
        assert "_" not in summary, f"raw field name leaked into: {summary}"


def test_durations_read_the_way_people_say_them():
    assert catalog.humanize_seconds(45) == "45s"
    assert catalog.humanize_seconds(600) == "10m"
    assert catalog.humanize_seconds(2700) == "45m"
    assert catalog.humanize_seconds(3600) == "1h"
    assert catalog.humanize_seconds(4200) == "1h 10m"


def test_ratios_are_shown_as_percentages():
    assert catalog.format_value(1.08, "ratio") == "108%"
    assert catalog.format_value(300, "seconds") == "5m"
    assert catalog.format_value(22, "count") == "22"
    assert catalog.format_value(None, "count") == "unknown"
