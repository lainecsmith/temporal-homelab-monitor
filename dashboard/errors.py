class GatewayError(Exception):
    """Anything that goes wrong talking to Temporal. kind is one of:
    unreachable | timeout | not_found | query_failed | invalid"""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind
