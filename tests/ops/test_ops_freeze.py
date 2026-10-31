from ams.ops import freeze


def test_add_lookup_expire_remove(tmp_path):
    p = tmp_path / "freeze.json"
    assert freeze.load(p, 0) == {}
    freeze.add(p, "asr", 3600, "tcc-host migration", now=1000)
    freeze.add(p, "*", 60, "everything", mute=False, now=1000)
    cur = freeze.load(p, 1010)
    assert freeze.lookup(cur, "asr").reason == "tcc-host migration"
    assert freeze.lookup(cur, "tts").service == "*" and not freeze.lookup(cur, "tts").mute
    assert freeze.lookup(freeze.load(p, 1100), "tts") is None  # "*" expired, asr still there
    assert freeze.lookup(freeze.load(p, 1100), "asr") is not None
    assert freeze.remove(p, "asr", now=1100) is True
    assert freeze.remove(p, "asr", now=1100) is False
    assert freeze.load(p, 1100) == {}


def test_garbage_is_ignored(tmp_path):
    p = tmp_path / "freeze.json"
    p.write_text('{"a": {"until": "x"}, "b": 3}')
    assert freeze.load(p, 0) == {}
    p.write_text("not json")
    assert freeze.load(p, 0) == {}
