from ams.ops.config import parse
from ams.ops.gate import Incident
from ams.ops.policy import choose


def svc(actions, sid="x"):
    cfg = parse(
        {
            "actions": {
                "advertise_tailscale": {"services": ["svc:dsh"]},
                "clean_tmp": {"dirs": ["/var/tmp/ams-watch-test"]},
            },
            "service": [
                {"id": sid, "launchd": f"gui/501/{sid}", "expect": "running", "actions": actions}
            ],
        }
    )
    return cfg.services[0]


def inc(kind):
    return Incident(kind, "x", f"x|{kind}", "h", "R")


def test_down_and_unhealthy_get_the_usual_cure_the_service_allows():
    assert choose(inc("job_down"), svc(["kickstart"])) == "kickstart"
    assert choose(inc("health"), svc(["kickstart"])) == "kickstart"
    assert (
        choose(inc("job_down"), svc(["soft_restart_kaorou", "kickstart"])) == "soft_restart_kaorou"
    )
    assert choose(inc("tailscale"), svc(["advertise_tailscale"])) == "advertise_tailscale"


def test_no_allowed_action_no_cure():
    assert choose(inc("job_down"), svc([])) is None
    assert choose(inc("job_down"), None) is None


def test_storms_logs_and_failed_interval_jobs_are_never_auto_fixed():
    for kind in ("job_flap", "log", "job_exit"):
        assert choose(inc(kind), svc(["kickstart", "soft_restart_kaorou"])) is None


def test_clean_tmp_is_never_chosen_by_rule():
    for kind in ("job_down", "health", "tailscale", "log"):
        assert choose(inc(kind), svc(["clean_tmp"])) is None
