import json
import sys

from ams.ops.runner import run
from ams.ops.store import Store


def test_store_roundtrip_and_change_gate(tmp_path):
    p = tmp_path / "s" / "state.json"
    s = Store(p)
    assert s.save() is True
    assert s.save() is False  # nothing changed -> no write
    s.data["sigs"]["a"] = {"count": 1}
    assert s.save() is True
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    again = Store(p)
    assert again.data["sigs"]["a"] == {"count": 1}
    assert again.data["version"] == 2


def test_store_keeps_new_default_keys_for_old_files(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 1, "sigs": {"x": {}}}))
    assert "outbox" in Store(p).data


def test_store_corrupt_file_is_set_aside(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{nope")
    s = Store(p)
    assert s.data["sigs"] == {}
    assert (tmp_path / "state.corrupt").exists()


def test_run_ok_timeout_and_missing():
    assert run([sys.executable, "-c", "print('hi')"]).out.strip() == "hi"
    assert run([sys.executable, "-c", "import sys; sys.exit(3)"]).rc == 3
    assert run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3).rc == 124
    assert run(["/nonexistent/binary"]).rc == 127
