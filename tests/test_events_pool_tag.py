"""``level-prefix`` classification and ``member_tag`` for the pool runner's
tagged log shape: ``LEVEL [<member>] <logger>: <msg>`` (e.g. "INFO [kvservice]
kvservice.main: started"). Several pooled services multiplex their lines onto
one stream; the harness must classify by the declared level, not by text
heuristics that see "error" inside a logger name like "uvicorn.error".
"""

from ams.events import Severity, classify, member_tag


def test_level_prefix_accepts_the_pool_runner_member_tag():
    def c(text: str) -> Severity:
        return classify(text, "stderr", "level-prefix")

    # The motivating case: "error" inside the logger name must not escalate
    # an INFO banner line the pool runner tagged with its member.
    assert c("INFO [pool] uvicorn.error: Started server process [123]") == Severity.INFO
    assert c("ERROR [kvservice] kvservice.main: boom") == Severity.ERROR
    assert c("WARNING [timeservice] sdk.acl: acl refresh failed") == Severity.WARNING
    # A tag may contain digits and hyphens.
    assert c("INFO [notification-svc2] notification.main: started") == Severity.INFO


def test_level_prefix_accepts_the_member_tag_under_auto_format():
    def c(text: str) -> Severity:
        return classify(text, "stderr", "auto")

    assert c("INFO [pool] uvicorn.error: Started server process [123]") == Severity.INFO
    assert c("ERROR [kvservice] kvservice.main: boom") == Severity.ERROR


def test_level_prefix_without_a_tag_is_unchanged():
    def c(text: str) -> Severity:
        return classify(text, "stderr", "level-prefix")

    assert c("INFO kvservice: started") == Severity.INFO
    assert c("ERROR kvservice: boom") == Severity.ERROR


def test_a_bracketed_token_without_a_level_still_uses_the_heuristics():
    def c(text: str) -> Severity:
        return classify(text, "stderr", "level-prefix")

    # No leading level token before "[kvservice]" -- not the pool runner's
    # shape, so this must fall through to the text heuristics like any other
    # unrecognized line, not be silently treated as a tagged INFO line.
    assert c("[kvservice] ERROR something failed") == Severity.ERROR
    assert c("[kvservice] server started") == Severity.INFO


def test_member_tag_reads_the_bracketed_token():
    assert member_tag("INFO [pool] uvicorn.error: Started server process [123]") == "pool"
    assert member_tag("ERROR [kvservice] kvservice.main: boom") == "kvservice"
    assert member_tag("WARNING [timeservice] sdk.acl: acl refresh failed") == "timeservice"
    assert member_tag("INFO [notification-svc2] notification.main: started") == "notification-svc2"


def test_member_tag_is_none_without_a_bracketed_tag():
    assert member_tag("INFO kvservice: started") is None
    assert member_tag("ERROR kvservice: boom") is None


def test_member_tag_is_none_when_the_line_has_no_level_prefix_shape():
    assert member_tag("[kvservice] something failed") is None
    assert member_tag("just a plain line") is None
    assert member_tag("Traceback (most recent call last):") is None
