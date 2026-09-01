from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("aegis_request_id")
