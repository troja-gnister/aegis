from __future__ import annotations

from .enums import JobState

_ALLOWED_TRANSITIONS = frozenset(
    {
        (JobState.QUEUED, JobState.RUNNING),
        (JobState.RUNNING, JobState.SUCCEEDED),
        (JobState.RUNNING, JobState.RETRY_WAIT),
        (JobState.RETRY_WAIT, JobState.RUNNING),
        (JobState.RUNNING, JobState.FAILED),
    }
)


def _job_state(value: object) -> JobState:
    if isinstance(value, JobState):
        return value
    if type(value) is str:
        try:
            return JobState(value)
        except ValueError:
            pass
    raise ValueError("invalid job state")


def assert_transition(source: object, target: object) -> None:
    transition = (_job_state(source), _job_state(target))
    if transition not in _ALLOWED_TRANSITIONS:
        raise ValueError("invalid job state transition")
