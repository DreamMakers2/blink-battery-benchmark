import logging

from blink_dashboard.logging_setup import SecretRedactionFilter


def test_dashboard_access_token_is_redacted_from_access_style_log() -> None:
    secret = "secret-dashboard-bearer"
    record = logging.LogRecord(
        "aiohttp.access",
        logging.INFO,
        __file__,
        1,
        f"127.0.0.1 GET /?token={secret} HTTP/1.1",
        (),
        None,
    )
    assert SecretRedactionFilter().filter(record)
    rendered = record.getMessage()
    assert secret not in rendered
    assert "[REDACTED]" in rendered
