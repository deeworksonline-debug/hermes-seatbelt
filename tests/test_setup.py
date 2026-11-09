"""The guided /seatbelt setup wizard, the one-time welcome and the friendlier help."""

from helpers import order


def _handler(rt, sb, answers):
    """A handler whose ask_choice replies come from `answers` (None = the user cancelled)."""
    asked = []
    queue = list(answers)

    def ask(question, choices):
        asked.append((question, [c["value"] for c in choices]))
        return queue.pop(0) if queue else None

    return sb.commands.make_handler(rt.state, ask), asked


def test_setup_applies_the_three_answers(rt, sb):
    handler, asked = _handler(rt, sb, ["25", "100", "1"])
    out = handler("setup", platform="cli")
    assert [len(c) for _, c in asked] == [4, 4, 4] and "Done. Your limits are set." in out and "PRACTICE mode" in out
    policy = sb.policy.load_policy(rt.state.overrides())
    assert (policy.max_order_usd, policy.ask_above_usd, policy.daily_max_notional_usd, policy.max_leverage) == (25.0, 12.5, 100.0, 1.0)
    assert rt.pre_tool_call("create_order", order(amount=0.001))["action"] == "block"          # $50 > the new $25 limit


def test_setup_changes_nothing_if_cancelled_midway(rt, sb):
    handler, _ = _handler(rt, sb, ["25", None])
    assert "nothing was changed" in handler("setup", platform="cli")
    assert rt.state.overrides() == {}


def test_setup_keep_everything_changes_nothing(rt, sb):
    handler, _ = _handler(rt, sb, ["keep", "keep", "keep"])
    assert "Nothing changed" in handler("setup", platform="cli")
    assert rt.state.overrides() == {}


def test_setup_never_leaves_daily_total_below_one_order(rt, sb):
    handler, _ = _handler(rt, sb, ["250", "100", "keep"])
    out = handler("setup", platform="cli")
    assert "raised to $250" in out
    assert sb.policy.load_policy(rt.state.overrides()).daily_max_notional_usd == 250.0


def test_setup_is_for_the_owner_only(rt, sb):
    handler, asked = _handler(rt, sb, ["25", "100", "1"])
    assert "only be used" in handler("setup", platform="telegram", source=None)
    assert not asked and rt.state.overrides() == {}


def test_setup_without_a_menu_explains_itself(rt, sb):
    assert "not available" in sb.commands.make_handler(rt.state, None)("setup", platform="cli")


def test_welcome_is_shown_once(rt, sb):
    handler = sb.commands.make_handler(rt.state)
    first = handler("status", platform="cli")
    second = handler("status", platform="cli")
    assert "Welcome to Seatbelt" in first and "Welcome to Seatbelt" not in second and "PAPER" in second


def test_help_mentions_the_friendly_commands(rt, sb):
    out = sb.commands.make_handler(rt.state)("help", platform="cli")
    assert "/seatbelt setup" in out and "practice mode" in out


def test_reset_usage_zeroes_todays_counters_but_keeps_limits(rt, sb):
    handler = sb.commands.make_handler(rt.state)
    handler("limits max_order_usd 90", platform="cli")
    for i in range(3):
        rt.pre_tool_call("create_order", order(amount=0.0005), tool_call_id=f"r{i}")
    assert rt.state.totals()[0] == 3
    assert "reset to zero" in handler("reset usage", platform="cli")
    assert rt.state.totals() == (0, 0.0)
    assert sb.policy.load_policy(rt.state.overrides()).max_order_usd == 90.0
    assert "only be used" in handler("reset usage", platform="telegram", source=None)
