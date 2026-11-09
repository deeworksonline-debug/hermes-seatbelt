"""Regression tests for the adversarial review. Each one reproduced a real defect before it was fixed."""

import json

import pytest

from helpers import order


def _live(rt):
    rt.state.set_mode("live")
    return rt


# --- sizing: worst case wins -------------------------------------------------------------------------
def test_decoy_notional_cannot_shrink_checked_size(rt):
    pm = {"exchange": "polymarket", "method": "createOrder", "confirmed": True,
          "params": {"market_id": "m", "side": "buy", "amount": 100000, "price": 0.5, "notional_usd": 1}}
    assert rt.pre_tool_call("pmxt_call", pm)["action"] == "block"
    for decoy in ("cost", "usd_amount", "quote_amount"):
        r = rt.pre_tool_call("create_order", order(amount=1, price=50000, **{decoy: 1}))
        assert r and r["action"] == "block", decoy


def test_decoy_aliases_cannot_hide_size_or_leverage(sb, rt):
    big = rt.pre_tool_call("create_order", order(amount=0.0001, price=50000, size=1))
    assert big["action"] == "block"
    args = order(amount=0.0005, leverage=1, lev=100)
    intent = sb.adapters.find_adapter(rt.adapters, "create_order", args).build_intent("create_order", args)
    assert intent.leverage == 100 and set(intent.leverage_keys) == {"leverage", "lev"}
    assert rt.pre_tool_call_modify("create_order", args) == {"action": "modify", "args": {"leverage": 3, "lev": 3}}


def test_market_order_price_is_not_trusted(rt):
    _live(rt)
    r = rt.pre_tool_call("create_order", order(amount=10, price=1, type="market"))
    assert r["action"] == "approve" and "could not be determined" in r["message"]
    assert rt.pre_tool_call("create_order", order(amount=10, price=0))["action"] == "approve"


def test_leverage_nested_or_garbled_is_not_missed(rt):
    base = order(amount=0.0005)
    assert rt.pre_tool_call("create_order", {**base, "params": {"leverage": 100}})["action"] == "block"
    assert rt.pre_tool_call_modify("create_order", {**base, "leverageRatio": 100}) == {"action": "modify", "args": {"leverageRatio": 3}}
    _live(rt)
    assert rt.pre_tool_call("create_order", {**base, "leverage": "max"})["action"] == "approve"
    assert rt.pre_tool_call("create_order", {**base, "deep": {"x": {"marginLeverage": 50}}})["action"] == "block"


def test_ask_keeps_the_leverage_clamp(rt):
    _live(rt)
    args = order(amount=0.0015, leverage=20)
    assert rt.pre_tool_call("create_order", args, tool_call_id="c1")["action"] == "approve"
    assert rt.pre_tool_call_modify("create_order", args, tool_call_id="c1") == {"action": "modify", "args": {"leverage": 3}}


def test_unknown_size_ask_is_never_a_blanket_always_allow(rt):
    _live(rt)
    a = rt.pre_tool_call("create_order", order(amount=10, type="market"), tool_call_id="a")
    b = rt.pre_tool_call("create_order", order(amount=999, type="market"), tool_call_id="b")
    assert a["rule_key"] != b["rule_key"]
    u1 = rt.pre_tool_call("mystery_buy_token", {"t": 1}, tool_call_id="u1")
    u2 = rt.pre_tool_call("mystery_buy_token", {"t": 2}, tool_call_id="u2")
    assert u1["rule_key"] != u2["rule_key"]


def test_pmxt_compound_method_names_are_caught(sb, rt):
    def find(method):
        return sb.adapters.find_adapter(rt.adapters, "pmxt_call", {"method": method, "exchange": "polymarket"})
    for risky in ("cancelAndCreateOrder", "getOrCreateOrder", "swapTokens", "SubmitOrder", "create_order"):
        assert find(risky) is not None, risky
    for safe in ("fetchMarkets", "cancelOrder", "watchOrderBook", "fetchBalance"):
        assert find(safe) is None, safe


def test_trade_detection_covers_more_names(sb):
    looks = sb.intent.looks_like_trade
    for name in ("open_long", "go_short", "get_and_buy", "quote_and_swap", "deposit_funds", "execute_swap",
                 "send_tokens", "redeem_shares"):
        assert looks(name), name
    for name in ("get_order_book", "short_url", "long_text_summary", "fetch_trades_history", "send_message", "read_file"):
        assert not looks(name), name


# --- budget: asks reserve, collisions ---------------------------------------------------------------
def test_parallel_asks_cannot_overshoot_daily_cap(rt, sb):
    _live(rt)
    sb.commands.make_handler(rt.state)("limits daily_max_orders 2", platform="cli")
    results = [rt.pre_tool_call("create_order", order(amount=0.0015), tool_call_id=f"p{i}") for i in range(3)]
    assert [r["action"] for r in results] == ["approve", "approve", "block"]


def test_denied_ask_releases_its_reservation(rt, sb):
    _live(rt)
    sb.commands.make_handler(rt.state)("limits daily_max_orders 1", platform="cli")
    rt.pre_tool_call("create_order", order(amount=0.0015), tool_call_id="d1")
    rt.post_tool_call("create_order", order(amount=0.0015), tool_call_id="d1", status="error")
    assert rt.pre_tool_call("create_order", order(amount=0.0015), tool_call_id="d2")["action"] == "approve"


def test_identical_asks_without_ids_are_each_counted(rt):
    _live(rt)
    args = order(amount=0.0015)
    rt.pre_tool_call("create_order", args)
    rt._verdicts.clear()
    rt.pre_tool_call("create_order", args)
    rt.post_tool_call("create_order", args, status="ok")
    rt.post_tool_call("create_order", args, status="ok")
    assert rt.state.totals()[0] == 2


# --- hook/middleware ordering (the critical finding) ------------------------------------------------
def test_paper_middleware_enforces_limits_even_when_it_runs_before_the_hook(rt):
    """Agent-loop order: tool_execution middleware wraps pre_tool_call, so it runs first and must not skip limits."""
    called = []
    out = json.loads(rt.tool_execution("create_order", order(amount=0.01), lambda a: called.append(a), tool_call_id="m1"))
    assert "Seatbelt stopped" in out["error"] and not called and rt.state.paper_summary()["fills"] == 0
    rt.state.set_halted(True)
    out = json.loads(rt.tool_execution("create_order", order(amount=0.0001), lambda a: called.append(a), tool_call_id="m2"))
    assert "HALTED" in out["error"] and rt.state.paper_summary()["fills"] == 0
    rt.state.set_halted(False)
    out = json.loads(rt.tool_execution("mystery_buy_token", {"t": 1}, lambda a: called.append(a), tool_call_id="m3"))
    assert "error" in out and not called


def test_hook_and_middleware_in_either_order_count_once(rt):
    ok = order(amount=0.0005)
    json.loads(rt.tool_execution("create_order", ok, lambda a: "REAL", tool_call_id="x1"))      # middleware first
    assert rt.pre_tool_call("create_order", ok, tool_call_id="x1") is None
    assert rt.state.totals()[0] == 1
    assert rt.pre_tool_call("create_order", ok, tool_call_id="x2") is None                       # hook first
    json.loads(rt.tool_execution("create_order", ok, lambda a: "REAL", tool_call_id="x2"))
    assert rt.state.totals()[0] == 2
    assert len([r for r in rt.state.recent(50) if r["decision"] == "allow"]) == 2


def test_paper_clamp_is_applied_to_the_simulated_fill(rt):
    out = json.loads(rt.tool_execution("create_order", order(amount=0.0005, leverage=20), lambda a: "REAL", tool_call_id="k1"))
    assert out["simulated_fill"]["leverage"] == 3 and out["seatbelt_adjusted"]


# --- tamper guard ------------------------------------------------------------------------------------
def test_multifile_patch_cannot_edit_seatbelt(rt, tmp_path):
    target = tmp_path / "plugin-data" / "seatbelt" / "x.py"
    body = f"*** Begin Patch\n*** Update File: {target}\n@@\n-a\n+b\n*** End Patch"
    assert rt.pre_tool_call("patch", {"patch": body})["action"] == "block"
    assert rt.pre_tool_call("patch", {"patch": "*** Begin Patch\n*** Update File: notes.txt\n@@\n-a\n+b\n*** End Patch"}) is None


@pytest.mark.parametrize("cmd", [
    "cd ~/.hermes/plugins && sed -i 's/x/y/' seatbelt/engine.py",
    "ls $HERMES_HOME/plugin-data/seatbelt",
    "cat //home//x/plugins//seatbelt//data.db",
    "cat '~/.hermes/plugins/seatbelt/x'",
    "hermes plugins disable seatbelt",
    "hermes -p work plugins disable --all",
    "hermes config set plugins.disabled seatbelt",
])
def test_tamper_command_variants_are_blocked(rt, cmd):
    assert rt.pre_tool_call("terminal", {"command": cmd})["action"] == "block", cmd


def test_hermes_config_file_is_protected(sb, tmp_path):
    cfg = tmp_path / "config.yaml"
    guard = sb.guard_self.SelfGuard([tmp_path / "plugin-data" / "seatbelt"], [cfg])
    assert guard.violates("write_file", {"path": str(cfg), "content": "x"})
    assert not guard.violates("write_file", {"path": str(tmp_path / "other.yaml"), "content": "x"})


def test_guard_has_few_false_positives(rt):
    for cmd in ("echo see plugins/seatbelt-notes", "python -m pytest tests", "git log --oneline", "ls ~/projects/seat"):
        assert rt.pre_tool_call("terminal", {"command": cmd}) is None, cmd


# --- redaction, limits, commands ---------------------------------------------------------------------
def test_redaction_covers_more_secrets(sb):
    text = sb.intent.redact_args({
        "privateKey": "AAA111", "seed_phrase": "BBB222", "pk": "CCC333", "Authorization": "DDD444",
        "headers": [{"cookie": "EEE555"}],
        "command": "curl -H 'Authorization: Bearer abcdefghijklmnop' --data 'api_key=FFF666'",
        "ok": "visible"})
    for secret in ("AAA111", "BBB222", "CCC333", "DDD444", "EEE555", "abcdefghijklmnop", "FFF666"):
        assert secret not in text, secret
    assert "visible" in text


@pytest.mark.parametrize("cmd", [
    "limits max_order_usd inf", "limits max_order_usd 1e999", "limits daily_max_orders 1e9",
    "limits max_order_usd -5", "limits unknown_trade_tools maybe", "limits ask_above_usd 500", "limits max_leverage 0",
])
def test_limits_reject_nonsense(rt, sb, cmd):
    assert "Invalid" in sb.commands.make_handler(rt.state)(cmd, platform="cli")
    assert rt.state.overrides() == {}


def test_local_ui_platforms_are_trusted_only_without_a_remote_source(rt, sb):
    handler = sb.commands.make_handler(rt.state)
    assert "LIVE" in handler("live I-ACCEPT-RISK", platform="tui", source=None)
    assert "only be used" in handler("resume", platform="telegram", source=None)


# --- found in the live Hermes run ---------------------------------------------------------------------
def test_local_gateway_platform_is_trusted(rt, sb):
    """`hermes chat` runs commands through the gateway's local adapter: platform == "local"."""
    class LocalSource:
        user_id = "sid:S-1-5-21-0"
    handler = sb.commands.make_handler(rt.state)
    assert "LIVE" in handler("live I-ACCEPT-RISK", platform="local", source=LocalSource())
    assert "only be used" in handler("resume", platform="telegram", source=LocalSource())


@pytest.mark.parametrize("cmd", [
    'hermes chat -q "/seatbelt live I-ACCEPT-RISK"',
    "hermes chat -q '/seatbelt limits max_order_usd 100000'",
    "echo /halt && hermes -z '/seatbelt resume'",
])
def test_agent_cannot_drive_seatbelt_commands_from_a_shell(rt, cmd):
    assert rt.pre_tool_call("terminal", {"command": cmd})["action"] == "block", cmd
    assert rt.pre_tool_call("execute_code", {"code": f"import os; os.system({cmd!r})"})["action"] == "block"


def test_slash_command_guard_has_no_false_positive_on_paths(rt):
    for cmd in ("ls /usr/local/bin", "cd /tmp/halting-problem && ls", "cat docs/seatbelt-notes.md"):
        assert rt.pre_tool_call("terminal", {"command": cmd}) is None, cmd
