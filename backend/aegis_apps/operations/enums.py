from django.db import models


class WorkerRole(models.TextChoices):
    OPERATIONS = "operations", "Operations"
    INDEXER = "indexer", "Indexer"
    MEDIA = "media", "Media"


class JobKind(models.TextChoices):
    FOUNDATION_PROBE = "foundation.probe", "Foundation probe"


class JobState(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    RETRY_WAIT = "retry_wait", "Retry wait"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


class HeartbeatStatus(models.TextChoices):
    IDLE = "idle", "Idle"
    RUNNING = "running", "Running"
    STOPPING = "stopping", "Stopping"


class SafeErrorCode(models.TextChoices):
    AUTHORIZATION_STALE = "authorization_stale", "Authorization stale"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted", "Attempts exhausted"
    HANDLER_FAILED = "handler_failed", "Handler failed"
    RETRYABLE_FAILURE = "retryable_failure", "Retryable failure"
