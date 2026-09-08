from __future__ import annotations

import pytest
from aegis_apps.operations.enums import JobKind, JobState, WorkerRole
from aegis_apps.operations.services import assert_transition


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (JobState.QUEUED, JobState.RUNNING),
        (JobState.RUNNING, JobState.SUCCEEDED),
        (JobState.RUNNING, JobState.RETRY_WAIT),
        (JobState.RETRY_WAIT, JobState.RUNNING),
        (JobState.RUNNING, JobState.FAILED),
    ],
)
def test_allowed_job_transitions(source: JobState, target: JobState) -> None:
    assert_transition(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (source, target)
        for source in JobState
        for target in JobState
        if (source, target)
        not in {
            (JobState.QUEUED, JobState.RUNNING),
            (JobState.RUNNING, JobState.SUCCEEDED),
            (JobState.RUNNING, JobState.RETRY_WAIT),
            (JobState.RETRY_WAIT, JobState.RUNNING),
            (JobState.RUNNING, JobState.FAILED),
        }
    ],
)
def test_every_other_job_transition_is_forbidden(source: JobState, target: JobState) -> None:
    with pytest.raises(ValueError, match="invalid job state transition"):
        assert_transition(source, target)


@pytest.mark.parametrize("value", ["", "unknown", 1, None, True])
def test_unknown_transition_values_fail_closed(value: object) -> None:
    with pytest.raises(ValueError, match="invalid job state"):
        assert_transition(value, JobState.RUNNING)
    with pytest.raises(ValueError, match="invalid job state"):
        assert_transition(JobState.RUNNING, value)


def test_phase_one_enum_values_are_stable() -> None:
    assert tuple(role.value for role in WorkerRole) == ("operations", "indexer", "media")
    assert tuple(state.value for state in JobState) == (
        "queued",
        "running",
        "retry_wait",
        "succeeded",
        "failed",
    )
    assert tuple(kind.value for kind in JobKind) == ("foundation.probe",)
