"""A port outside 1-65535 must not crash the launcher with a traceback."""

import pytest

from scripts import run_server


@pytest.mark.parametrize("port", [65536, 99999, -1])
def test_out_of_range_port_falls_back_to_a_free_one(port):
    with run_server.reserve_available_socket(port, host="127.0.0.1") as listener:
        active = listener.getsockname()[1]
    assert 0 < active <= 65535
    notice = run_server.port_switch_notice(port, active)
    assert notice is not None and str(port) in notice and "1 到 65535" in notice


def test_notice_for_a_busy_port_is_unchanged():
    assert "无法绑定" in run_server.port_switch_notice(8080, 8081)
    assert run_server.port_switch_notice(8080, 8080) is None
    assert run_server.port_switch_notice(0, 51234) is None
