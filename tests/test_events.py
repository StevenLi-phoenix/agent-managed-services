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


# ------------------------------------------------------- per-declaration formats


def test_level_prefix_format_reads_the_leading_level_token():
    def c(text: str) -> Severity:
        return classify(text, "stderr", "level-prefix")

    assert c("DEBUG ams.svc: tick") == Severity.DEBUG
    assert c("INFO kvservice: started") == Severity.INFO
    assert c("WARNING kvservice: disk almost full") == Severity.WARNING
    assert c("warn ams.sdk: lower case is accepted") == Severity.WARNING
    assert c("ERROR kvservice: boom") == Severity.ERROR
    assert c("CRITICAL kvservice: boom") == Severity.CRITICAL
    assert c("FATAL kvservice: boom") == Severity.CRITICAL


def test_level_prefix_beats_the_heuristics_on_the_message_body():
    # The heuristics call both of these ERROR ("uvicorn.error", "Traceback");
    # the declared format says the service already told us the level.
    def c(text: str) -> Severity:
        return classify(text, "stderr", "level-prefix")

    assert c("INFO uvicorn.error: request served") == Severity.INFO
    assert c("INFO svc: Traceback (most recent call last) in a docstring") == Severity.INFO
    # A line without the prefix (uvicorn's own banner, a traceback body) still
    # gets the heuristics rather than being silently downgraded.
    assert c("Traceback (most recent call last):") == Severity.ERROR


def test_json_format_trusts_level_or_severity_and_nothing_else():
    def c(text: str) -> Severity:
        return classify(text, "stderr", "json")

    assert c('{"level":"debug","msg":"x"}') == Severity.DEBUG
    assert c('{"level":"info","msg":"x"}') == Severity.INFO
    assert c('{"level":"warn","msg":"x"}') == Severity.WARNING  # caddy spells it warn
    assert c('{"level":"error","msg":"x"}') == Severity.ERROR
    assert c('{"level":"panic","msg":"x"}') == Severity.CRITICAL
    assert c('{"level":"fatal","msg":"x"}') == Severity.CRITICAL
    assert c('{"severity":"WARNING","message":"x"}') == Severity.WARNING
    # the message body is data, never a severity source
    assert c('{"level":"info","msg":"ERROR: connection refused"}') == Severity.INFO
    assert c('{"level":"info","logger":"Traceback"}') == Severity.INFO


def test_json_format_falls_back_to_the_heuristics_when_unusable():
    def c(text: str) -> Severity:
        return classify(text, "stderr", "json")

    assert c('{"level": ') == Severity.INFO  # truncated json
    assert c('{"msg":"ERROR here"}') == Severity.ERROR  # object with no level
    assert c('{"level":42}') == Severity.INFO  # level is not a name we know
    assert c('["level","error"]') == Severity.ERROR  # not an object
    assert c("plain WARNING line") == Severity.WARNING  # not json at all


def test_auto_tries_prefix_then_json_then_heuristics():
    assert classify("INFO uvicorn.error: served", "stderr") == Severity.INFO
    assert classify('{"level":"info","msg":"ERROR: nope"}', "stderr") == Severity.INFO
    assert classify("plain ERROR line", "stderr") == Severity.ERROR
    # plain never looks at structure at all
    assert classify('{"level":"info","msg":"ERROR: nope"}', "stderr", "plain") == Severity.ERROR
    assert classify("INFO uvicorn.error: served", "stderr", "plain") == Severity.ERROR


def test_logline_from_raw_honours_the_format_hint():
    raw = b'{"level":"info","msg":"ERROR: nope"}'
    assert LogLine.from_raw("svc", "stderr", raw).severity == Severity.INFO  # auto
    assert LogLine.from_raw("svc", "stderr", raw, "json").severity == Severity.INFO
    assert LogLine.from_raw("svc", "stderr", raw, "plain").severity == Severity.ERROR


def test_an_expected_exit_is_informational():
    crashed = ServiceExited("svc", 1, exit_code=None, signal=15, uptime_s=3.0)
    stopped = ServiceExited("svc", 1, exit_code=None, signal=15, uptime_s=3.0, expected=True)
    assert not crashed.ok and not stopped.ok
    assert severity_of(crashed) == Severity.ERROR
    assert severity_of(stopped) == Severity.INFO
    assert stopped.severity == Severity.INFO
