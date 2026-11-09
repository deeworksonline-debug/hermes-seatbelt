import json

from helpers import order


# ---------------------------------------------------------------- adapters / intent
def test_generic_adapter_builds_intent(sb, rt):
    adapter = sb.adapters.find_adapter(rt.adapters, "create_order", order(leverage=5))
    intent = adapter.build_intent("create_order", order(leverage=5))
    assert intent.venue == "bybit" and intent.symbol == "BTC"
    assert intent.notional_usd == 50.0 and intent.leverage == 5 and intent.leverage_keys == ("leverage",)


def test_pmxt_call_only_matches_state_changing_methods(sb, rt):
    read = {"method": "fetchMarkets", "exchange": "polymarket", "params": {}}
    write = {"method": "createOrder", "exchange": "polymarket",
             "params": {"market_id": "m1", "side": "buy", "amount": 40, "price": 0.5}, "confirmed": True}
    cancel = {"method": "cancelOrder", "exchange": "polymarket", "params": {"order_id": "x"}}
    assert sb.adapters.find_adapter(rt.adapters, "pmxt_call", read) is None
    assert sb.adapters.find_adapter(rt.adapters, "pmxt_call", cancel) is None
    adapter = sb.adapters.find_adapter(rt.adapters, "pmxt_call", write)
    intent = adapter.build_intent("pmxt_call", write)
    assert intent.kind == "prediction" and intent.venue == "polymarket" and intent.notional_usd == 20.0


def test_market_order_without_price_uses_upper_bound(sb, rt):
    args = {"market_id": "m", "side": "buy", "amount": 80, "exchange": "polymarket"}
    intent = sb.adapters.find_adapter(rt.adapters, "pmxt_order", args).build_intent("pmxt_order", args)
    assert intent.notional_usd == 80.0      # worst case $1 per share


def test_unparseable_size_is_unknown_not_zero(sb, rt):
    args = order(amount="lots", price="cheap")
    intent = sb.adapters.find_adapter(rt.adapters, "create_order", args).build_intent("create_order", args)
    assert intent.notional_usd is None


def test_nft_price_in_eth_needs_a_known_rate(sb, rt):
    args = {"contract": "0xabc", "token_id": "7", "price": 0.02, "currency": "ETH"}
    adapter = sb.adapters.find_adapter(rt.adapters, "buy_nft", args)
    assert adapter.build_intent("buy_nft", args).notional_usd is None
    assert abs(adapter.build_intent("buy_nft", args, {"ETH": 2500}).notional_usd - 50.0) < 1e-9


def test_trade_name_heuristic(sb):
    looks = sb.intent.looks_like_trade
    assert looks("place_perp_order") and looks("swap_tokens") and looks("mcp_bybit_withdraw")
    assert not looks("get_order_book") and not looks("read_file") and not looks("terminal")


def test_audit_preview_redacts_secrets(sb):
    text = sb.intent.redact_args({"symbol": "BTC", "api_key": "SUPERSECRET", "nested": {"private_key": "0xdead"}})
    assert "SUPERSECRET" not in text and "0xdead" not in text and "BTC" in text


# ---------------------------------------------------------------- engine
def _decide(sb, rt, args, **kw):
    adapter = sb.adapters.find_adapter(rt.adapters, "create_order", args)
    intent = adapter.build_intent("create_order", args)
    return sb.engine.decide(intent, sb.policy.load_policy(), orders_today=kw.get("orders", 0),
                            notional_today=kw.get("notional", 0.0), paper=kw.get("paper", False))


def test_small_allowed_order(sb, rt):
    assert _decide(sb, rt, order(amount=0.0005)).action == "allow"           # $25


def test_large_order_asks_and_huge_order_blocks(sb, rt):
    assert _decide(sb, rt, order(amount=0.0015)).action == "ask"             # $75
    assert _decide(sb, rt, order(amount=0.01)).action == "block"             # $500


def test_leverage_is_clamped_not_blocked(sb, rt):
    d = _decide(sb, rt, order(amount=0.0005, leverage=50))
    assert d.action == "clamp" and d.modified_args == {"leverage": 3}


def test_unlisted_symbol_asks(sb, rt):
    d = _decide(sb, rt, order(symbol="DOGE", amount=100, price=0.2))
    assert d.action == "ask" and "DOGE" in d.reason


def test_daily_limits_block(sb, rt):
    assert _decide(sb, rt, order(amount=0.0005), orders=10).action == "block"
    assert _decide(sb, rt, order(amount=0.0005), notional=480).action == "block"


def test_unknown_size_asks(sb, rt):
    assert _decide(sb, rt, order(amount="?", price="?")).action == "ask"


def test_paper_mode_does_not_interrupt_for_asks(sb, rt):
    assert _decide(sb, rt, order(amount=0.0015), paper=True).action == "allow"
    assert _decide(sb, rt, order(amount=0.01), paper=True).action == "block"  # hard limits still apply


# ---------------------------------------------------------------- runtime (hooks)
def test_non_trading_tools_are_untouched(rt):
    assert rt.pre_tool_call("read_file", {"path": "x"}) is None
    assert rt.pre_tool_call("terminal", {"command": "ls"}) is None
    assert rt.pre_tool_call("pmxt_call", {"method": "fetchMarkets", "exchange": "polymarket"}) is None


def test_block_message_is_actionable(rt):
    result = rt.pre_tool_call("create_order", order(amount=0.01))
    assert result["action"] == "block"
    msg = result["message"]
    assert "Seatbelt stopped" in msg and "per-order limit" in msg and "remaining budget" in msg and "/seatbelt" in msg


def test_ask_returns_stable_rule_key(rt):
    rt.state.set_mode("live")
    a = rt.pre_tool_call("create_order", order(amount=0.0015))
    b = rt.pre_tool_call("create_order", order(amount=0.0019))
    assert a["action"] == b["action"] == "approve" and a["rule_key"] == b["rule_key"]


def test_clamp_returns_modify(rt):
    assert rt.pre_tool_call("create_order", order(amount=0.0005, leverage=20)) is None
    result = rt.pre_tool_call_modify("create_order", order(amount=0.0005, leverage=20))
    assert result == {"action": "modify", "args": {"leverage": 3}}


def test_daily_counters_accumulate_and_then_block(rt):
    for _ in range(10):
        assert rt.pre_tool_call("create_order", order(amount=0.0002)) is None   # $10 each
    assert rt.pre_tool_call("create_order", order(amount=0.0002))["action"] == "block"


def test_ask_is_counted_only_after_it_runs(rt):
    rt.state.set_mode("live")
    args = order(amount=0.0015)
    rt.pre_tool_call("create_order", args, tool_call_id="t1")
    assert rt.state.totals() == (0, 0.0)                          # not counted yet
    rt.post_tool_call("create_order", args, tool_call_id="t1", status="ok")
    assert rt.state.totals()[0] == 1
    rt.pre_tool_call("create_order", args, tool_call_id="t2")      # denied: tool never ran
    rt.post_tool_call("create_order", args, tool_call_id="t2", status="error")
    assert rt.state.totals()[0] == 1


def test_halt_blocks_all_trading_and_survives(rt):
    rt.state.set_halted(True)
    assert "HALTED" in rt.pre_tool_call("create_order", order(amount=0.0001))["message"]
    assert rt.pre_tool_call("read_file", {"path": "x"}) is None    # non-trading tools still work
    rt.state.set_halted(False)
    assert rt.pre_tool_call("create_order", order(amount=0.0001)) is None


def test_unknown_trading_tool_blocked_in_paper_asks_in_live(rt):
    r = rt.pre_tool_call("mystery_buy_token", {"token": "X"})
    assert r["action"] == "block" and "no adapter" in r["message"]
    rt.state.set_mode("live")
    assert rt.pre_tool_call("mystery_buy_token", {"token": "X"})["action"] == "approve"
    assert rt.state.learn_list()[0]["tool"] == "mystery_buy_token"


def test_execute_code_bypass_is_caught(rt):
    code = "from hermes_pmxt.tools import pmxt_order\npmxt_order('m','yes',1000,'buy','polymarket',confirmed=True)"
    assert rt.pre_tool_call("execute_code", {"code": code})["action"] == "block"          # paper mode
    assert rt.pre_tool_call("execute_code", {"code": "print(sum(range(10)))"}) is None
    curl = "curl -X POST https://api.bybit.com/v5/order/create -d '{}'"
    assert rt.pre_tool_call("terminal", {"command": curl})["action"] == "block"
    assert rt.pre_tool_call("terminal", {"command": "curl https://api.bybit.com/v5/market/tickers"}) is None


def test_internal_error_fails_closed_for_trading_only(rt, monkeypatch):
    monkeypatch.setattr(rt, "_compute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert rt.pre_tool_call("create_order", order())["action"] == "block"
    assert rt.pre_tool_call("read_file", {"path": "x"}) is None


# ---------------------------------------------------------------- paper mode
def test_paper_mode_simulates_and_never_calls_real_tool(rt):
    called = []
    out = rt.tool_execution("create_order", order(amount=0.0005), lambda a: called.append(a) or "REAL")
    assert not called and json.loads(out)["seatbelt_paper_mode"] is True
    assert rt.state.paper_summary()["fills"] == 1


def test_live_mode_passes_through(rt):
    rt.state.set_mode("live")
    assert rt.tool_execution("create_order", order(amount=0.0005), lambda a: "REAL") == "REAL"


def test_non_trading_tool_passes_through_in_paper_mode(rt):
    assert rt.tool_execution("read_file", {"path": "x"}, lambda a: "FILE") == "FILE"


def test_paper_failure_does_not_fall_through_to_real(rt, monkeypatch):
    monkeypatch.setattr(rt.state, "add_paper_fill", lambda **k: (_ for _ in ()).throw(RuntimeError("db")))
    out = rt.tool_execution("create_order", order(amount=0.0005), lambda a: "REAL")
    assert out != "REAL" and "error" in json.loads(out)


# ---------------------------------------------------------------- tamper guard
def test_agent_cannot_touch_seatbelt_files_or_disable_it(rt, tmp_path):
    protected = tmp_path / "plugin-data" / "seatbelt" / "data.db"
    assert rt.pre_tool_call("write_file", {"path": str(protected), "content": "x"})["action"] == "block"
    assert rt.pre_tool_call("terminal", {"command": f"rm -f {protected}"})["action"] == "block"
    assert rt.pre_tool_call("terminal", {"command": "hermes plugins disable seatbelt"})["action"] == "block"
    assert rt.pre_tool_call("terminal", {"command": "sqlite3 ~/.hermes/plugin-data/seatbelt/data.db 'update kv set value=1'"})["action"] == "block"
    assert rt.pre_tool_call("write_file", {"path": str(tmp_path / "notes.txt"), "content": "x"}) is None


# ---------------------------------------------------------------- commands
def _cmd(rt, sb, text, platform="cli", source=None):
    return sb.commands.make_handler(rt.state)(text, platform=platform, source=source)


def test_commands_status_log_halt_resume(rt, sb):
    rt.pre_tool_call("create_order", order(amount=0.01))
    assert "BLOCK" in _cmd(rt, sb, "log")
    assert "HALTED" in _cmd(rt, sb, "halt") and rt.state.is_halted()
    assert "resumed" in _cmd(rt, sb, "resume") and not rt.state.is_halted()
    assert "PAPER" in _cmd(rt, sb, "status")


def test_live_requires_exact_confirmation(rt, sb):
    assert "confirm" in _cmd(rt, sb, "live").lower() and rt.state.mode() == "paper"
    _cmd(rt, sb, "live I-ACCEPT-RISK")
    assert rt.state.mode() == "live"
    _cmd(rt, sb, "paper")
    assert rt.state.mode() == "paper"


def test_limits_validated_and_applied(rt, sb):
    assert "Invalid" in _cmd(rt, sb, "limits max_order_usd abc")
    assert "Unknown limit" in _cmd(rt, sb, "limits admin_user_ids 1")
    _cmd(rt, sb, "limits max_order_usd 1000")
    _cmd(rt, sb, "limits ask_above_usd 900")
    assert rt.pre_tool_call("create_order", order(amount=0.01)) is None          # $500 now allowed
    _cmd(rt, sb, "reset")
    assert rt.pre_tool_call("create_order", order(amount=0.01))["action"] == "block"


def test_sensitive_commands_need_a_trusted_caller(rt, sb):
    class Source:
        user_id = "stranger"
    assert "only be used" in _cmd(rt, sb, "live I-ACCEPT-RISK", platform="telegram", source=Source())
    assert rt.state.mode() == "paper"
    assert "HALTED" in _cmd(rt, sb, "halt", platform="telegram", source=Source())   # halting is always allowed
    assert "only be used" in _cmd(rt, sb, "resume", platform="telegram", source=None)
    rt.state.set_override("admin_user_ids", ["boss"])


def test_register_wires_everything(sb, monkeypatch):
    import seatbelt_plugin

    class Ctx:
        def __init__(self):
            self.hooks, self.mw, self.cmds = [], [], []
        def register_hook(self, name, cb, **k): self.hooks.append(name)
        def register_middleware(self, kind, cb): self.mw.append(kind)
        def register_command(self, name, handler, **k): self.cmds.append(name)

    ctx = Ctx()
    seatbelt_plugin.register(ctx)
    assert set(ctx.hooks) == {"pre_tool_call", "post_tool_call"}
    assert ctx.mw == ["tool_execution"] and set(ctx.cmds) == {"seatbelt", "halt"}
