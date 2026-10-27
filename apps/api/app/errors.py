"""Transport-independent domain failures."""
class DomainError(Exception):
    def __init__(self, code: str, message: str = '', status_code: int = 409,
                 retryable: bool = False, details: dict | None = None):
        self.code = code
        self.status_code = status_code
        self.message = message
        self.retryable = retryable
        self.details = dict(details or {})
        super().__init__(message or code)
