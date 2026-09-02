import os
import sys

import pytest

IS_LINUX = sys.platform.startswith("linux")


def _has_delegated_cgroup() -> bool:
    try:
        cg = "/sys/fs/cgroup" + open("/proc/self/cgroup").read().strip().split(":", 2)[2]
        return os.access(cg, os.W_OK) and os.path.exists(cg + "/cgroup.subtree_control")
    except (OSError, IndexError):
        return False


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="needs Linux host with userns + delegated cgroup")
    for item in items:
        if "linux" in item.keywords and not (IS_LINUX and _has_delegated_cgroup()):
            item.add_marker(skip)
