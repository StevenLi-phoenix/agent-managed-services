from ams.ops.text import clip, redact, sanitize, signature, strip_controls


def test_redacts_bearer_and_prefixed_keys():
    assert "abcdefghij" not in redact("Authorization: Bearer abcdefghijklmnop")
    assert "llmgw_" not in redact("key llmgw_0123456789abcdef used")
    assert "pat_" not in redact("pat_FAKEFAKExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")


def test_redacts_key_value_pairs_and_hex_and_jwt():
    out = redact("GET /x?token=supersecretvalue&a=1 SESSDATA=abcd1234 key: hunter2hunter2")
    assert "supersecretvalue" not in out and "abcd1234" not in out and "hunter2" not in out
    assert "<redacted-hex>" in redact("id " + "a" * 40)
    assert "<redacted-jwt>" in redact("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.sigsigsig")


def test_redact_leaves_ordinary_text():
    s = "watch: feed fetch HTTP Error 404: Not Found (attempt 1/4), retrying in 0.3s"
    assert redact(s) == s


def test_strip_controls_and_clip():
    assert strip_controls("a\x1b[31mb\tc\nd\x07") == "a[31mb c d"
    assert clip("x" * 10, 5) == "xxxx…"
    assert sanitize("ok \x1b token=zzzzzzzz", 100) == "ok  token=<redacted>"


def test_signature_collapses_channel_ids_numbers_and_timestamps():
    a = "2026-10-30 23:48:08,615 WARNING watch: RSS feed for UCQSEAbOs6vsJfy7WN7iYaGQ failed after 4 attempts"
    b = "2026-10-31 01:02:03,111 WARNING watch: RSS feed for UCpWFwhNVukgTRJGNVOIrfTg failed after 3 attempts"
    assert signature("kaorou-daemon", a) == signature("kaorou-daemon", b)
    assert signature("kaorou-daemon", a) != signature("kaorou-api", a)


def test_signature_collapses_youtube_video_ids():
    a = "ERROR: [youtube] eNabNrWjNPI: This live event will begin in a few moments."
    b = "ERROR: [youtube] PvisWSLNBNM: This live event will begin in a few moments."
    assert signature("d", a) == signature("d", b)


def test_signature_of_json_logs_ignores_the_numbers_inside():
    a = '{"level":"warn","msg":"dropped invalid upstream items","src":"gdelt-geo","articles":6494,"ts":"2026-10-30T18:30:03Z"}'
    b = '{"level":"warn","msg":"dropped invalid upstream items","src":"gdelt-geo","articles":7001,"ts":"2026-10-31T01:00:00Z"}'
    c = '{"level":"warn","msg":"dropped invalid upstream items","src":"other"}'
    assert signature("ga", a) == signature("ga", b) != signature("ga", c)
    assert signature("ga", "{not json") != signature("ga", "{not json either")
