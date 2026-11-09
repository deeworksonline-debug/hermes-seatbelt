"""An 'ask' only means something if a person decides. Found in a live run: Hermes' default 'smart' approval
mode let an AI guardian approve Seatbelt's request without the human ever being asked."""

import sys
import types

import pytest

from helpers import order

MID = order(amount=0.0015)      # $75: between the $50 ask line and the $100 block line


@pytest.mark.parametrize("mode", ["smart", "off", "unknown", "yolo"])
def test_ask_becomes_a_block_when_a_person_is_not_guaranteed(rt, mode):
    rt.state.set_mode("live")
    rt.approval_mode_fn = lambda: mode
    result = rt.pre_tool_call("create_order", MID, tool_call_id="a1")
    assert result["action"] == "block"
    assert f"approvals.mode = {mode}" in result["message"] and "hermes config set approvals.mode manual" in result["message"]
    assert rt.state.totals() == (0, 0.0)                         # nothing reserved for a blocked order


def test_ask_stays_an_ask_in_manual_mode(rt):
    rt.state.set_mode("live")
    rt.approval_mode_fn = lambda: "manual"
    assert rt.pre_tool_call("create_order", MID, tool_call_id="a2")["action"] == "approve"


def test_opting_out_lets_hermes_handle_asks(rt, sb):
    rt.state.set_mode("live")
    rt.approval_mode_fn = lambda: "smart"
    assert "True" in sb.commands.make_handler(rt.state)("limits require_manual_approvals true", platform="cli")
    assert "False" in sb.commands.make_handler(rt.state)("limits require_manual_approvals false", platform="cli")
    assert rt.pre_tool_call("create_order", MID, tool_call_id="a3")["action"] == "approve"


def test_unknown_trading_tool_is_blocked_not_asked_without_a_person(rt):
    rt.state.set_mode("live")
    rt.approval_mode_fn = lambda: "smart"
    result = rt.pre_tool_call("mystery_buy_token", {"t": 1}, tool_call_id="u1")
    assert result["action"] == "block" and "approvals.mode = smart" in result["message"]


def test_hard_limits_and_paper_mode_do_not_depend_on_the_approval_mode(rt):
    rt.approval_mode_fn = lambda: "off"
    assert rt.pre_tool_call("create_order", order(amount=0.01), tool_call_id="h1")["action"] == "block"
    assert rt.pre_tool_call("create_order", order(amount=0.0005), tool_call_id="h2") is None        # allowed in paper


def test_status_tells_the_user_what_will_happen(rt, sb, monkeypatch):
    from seatbelt_plugin import hermes_env
    monkeypatch.setattr(hermes_env, "approval_mode", lambda: "smart")
    out = sb.commands.make_handler(rt.state)("status", platform="cli")
    assert "BLOCKED" in out and "hermes config set approvals.mode manual" in out
    monkeypatch.setattr(hermes_env, "approval_mode", lambda: "manual")
    assert "will ask YOU" in sb.commands.make_handler(rt.state)("status", platform="cli")


def _fake_hermes_home(monkeypatch, tmp_path, yaml_text):
    if yaml_text is not None:
        (tmp_path / "config.yaml").write_text(yaml_text, encoding="utf-8")
    module = types.ModuleType("hermes_constants")
    module.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", module)


@pytest.mark.parametrize("text,expected", [
    (None, "smart"),                                    # no file: Hermes' own default
    ("model:\n  default: x\n", "smart"),                # approvals not set: default
    ("approvals:\n  mode: manual\n", "manual"),
    ("approvals:\n  mode: OFF\n", "off"),
    ("approvals: [broken\n", "unknown"),                # unreadable: never assume a human
])
def test_reads_the_real_hermes_setting(sb, monkeypatch, tmp_path, text, expected):
    from seatbelt_plugin import hermes_env
    _fake_hermes_home(monkeypatch, tmp_path, text)
    assert hermes_env.approval_mode() == expected


def test_missing_hermes_means_unknown(monkeypatch):
    from seatbelt_plugin import hermes_env
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    assert hermes_env.approval_mode() == "unknown"
