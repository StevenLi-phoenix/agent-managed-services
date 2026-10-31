import copy

import pytest

from ams.ops.config import ConfigError, load, parse

GOOD = {
    "ops": {"tick_s": 10},
    "service": [
        {
            "id": "asr",
            "launchd": "gui/501/com.asr.service",
            "expect": "running",
            "logs": ["~/x.log"],
            "ignore": ["^noise"],
            "health_url": "http://127.0.0.1:8099/health",
            "actions": ["kickstart"],
        },
        {"id": "dsh", "launchd": "system/app.lishuyu.dsh-events", "expect": "running"},
    ],
}


def test_defaults_and_expansion():
    cfg = parse(GOOD)
    assert cfg.mode == "live" and cfg.tick_s == 10
    assert cfg.forward.url == "http://127.0.0.1:18090/escalate" and cfg.forward.origin == "mini"
    assert not hasattr(cfg, "llm")
    asr = cfg.service("asr")
    assert asr and asr.label == "com.asr.service" and asr.domain == "gui/501"
    assert str(asr.logs[0]).startswith("/")  # ~ expanded
    assert asr.ignore[0].search("noise here")


@pytest.mark.parametrize(
    "mutate,needle",
    [
        (lambda r: r["service"][0].update(id="Bad Id"), "service.id"),
        (lambda r: r["service"][0].update(expect="always"), "expect"),
        (lambda r: r["service"][0].update(actions=["rm_rf"]), "unknown"),
        (lambda r: r["service"][0].update(launchd="nope"), "launchd"),
        (lambda r: r["service"][0].update(ignore=["("]), "bad regex"),
        (lambda r: r["service"][1].update(actions=["kickstart"]), "system-domain"),
        (lambda r: r["service"][0].update(typo=1), "unknown key"),
        (lambda r: r.update(bogus={}), "unknown key"),
        (lambda r: r.update(llm={}), "unknown key"),  # the model layer is gone
        (lambda r: r["ops"].update(mode="yolo"), "ops.mode"),
        (lambda r: r["ops"].update(digest_hours=[9]), "unknown key"),
        (lambda r: r["service"].append({"id": "asr"}), "duplicate"),
    ],
)
def test_rejects(mutate, needle):
    raw = copy.deepcopy(GOOD)
    mutate(raw)
    with pytest.raises(ConfigError, match=needle):
        parse(raw)


def test_tailscale_and_clean_tmp_actions_need_their_tables():
    with pytest.raises(ConfigError, match="advertise_tailscale"):
        parse({"service": [{"id": "x", "actions": ["advertise_tailscale"]}]})
    with pytest.raises(ConfigError, match="clean_tmp"):
        parse({"service": [{"id": "x", "actions": ["clean_tmp"]}]})
    cfg = parse(
        {
            "actions": {"advertise_tailscale": {"services": ["svc:dsh"]}},
            "service": [{"id": "x", "actions": ["advertise_tailscale"]}],
        }
    )
    assert cfg.tailscale.services == ("svc:dsh",)


def test_load_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "nope.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("= nope")
    with pytest.raises(ConfigError):
        load(bad)
    ok = tmp_path / "ok.toml"
    ok.write_text('[[service]]\nid = "a"\n')
    assert load(ok).config_dir == tmp_path


def test_the_shipped_example_parses_and_keeps_the_kaorou_guard():
    from pathlib import Path

    cfg = load(Path(__file__).resolve().parents[2] / "examples" / "watch" / "watch.toml")
    assert cfg.service("ams") and cfg.service("ams").actions == ()
    daemon = cfg.service("kaorou-daemon")
    assert "kickstart" not in daemon.actions and daemon.actions == ("soft_restart_kaorou",)
