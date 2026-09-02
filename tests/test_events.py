from ams.events import (
    MAX_LINE_BYTES,
    HealthChanged,
    LogLine,
    OrphanReaped,
    ServiceExited,
    Severity,
    classify,
    severity_of,
)


def test_classify_markers():
    assert classify("2024 INFO started", "stdout") == Severity.INFO
    assert classify("something WARNING here", "stdout") == Severity.WARNING
    assert classify("warn: low disk", "stderr") == Severity.WARNING
    assert classify("DeprecationWarning: x", "stderr") == Severity.WARNING
    assert classify("ERROR: failed", "stdout") == Severity.ERROR
    assert classify("Traceback (most recent call last):", "stderr") == Severity.ERROR
    assert classify("ValueError: bad thing", "stderr") == Severity.ERROR
    assert classify("FATAL: cannot bind", "stderr") == Severity.CRITICAL
    assert classify("DEBUG poll tick", "stderr") == Severity.DEBUG
    # stderr with no marker is INFO: most servers log everything to stderr.
    assert classify("Serving HTTP on 0.0.0.0 port 8000", "stderr") == Severity.INFO


def test_classify_most_severe_wins():
    assert classify("WARNING: ERROR-like text", "stdout") == Severity.ERROR
    assert classify("error then fatal", "stdout") == Severity.CRITICAL


def test_logline_from_raw_decodes_strips_and_truncates():
    line = LogLine.from_raw("svc", "stdout", b"hello \xff world\r\n")
    assert line.text == "hello � world"
    assert line.service_id == "svc" and line.stream == "stdout"
    assert line.severity == Severity.INFO and not line.truncated

    big = LogLine.from_raw("svc", "stderr", b"E" * (MAX_LINE_BYTES + 10))
    assert big.truncated and len(big.text) == MAX_LINE_BYTES


def test_service_exited_semantics():
    ok = ServiceExited("svc", 1, exit_code=0, signal=None, uptime_s=1.0)
    bad = ServiceExited("svc", 1, exit_code=1, signal=None, uptime_s=1.0)
    killed = ServiceExited("svc", 1, exit_code=None, signal=9, uptime_s=1.0)
    assert ok.ok and not bad.ok and not killed.ok
    assert severity_of(ok) == Severity.INFO
    assert severity_of(bad) == Severity.ERROR
    assert severity_of(killed) == Severity.ERROR


def test_other_event_severities():
    assert severity_of(HealthChanged("svc", healthy=False)) == Severity.WARNING
    assert severity_of(HealthChanged("svc", healthy=True)) == Severity.INFO
    assert severity_of(OrphanReaped(4242, 0, None)) == Severity.INFO
