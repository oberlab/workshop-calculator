"""Logging without personal data: the access log leaves out client addresses and query strings.

Query strings can hold dates, notices and the login provider's one-time code; addresses are
personal data. The reverse proxy keeps its own access log. `uvicorn-log.json` uses this formatter.
"""

from copy import copy

from uvicorn.logging import AccessFormatter


class PrivateAccessFormatter(AccessFormatter):
    def formatMessage(self, record):  # noqa: N802 - logging's name
        record = copy(record)
        _client, method, full_path, http_version, status_code = record.args
        record.args = ("-", method, full_path.split("?", 1)[0], http_version, status_code)
        return super().formatMessage(record)
