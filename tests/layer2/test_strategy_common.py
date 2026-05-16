from layer2.strategy_common import invert_signal, dollar_per_unit


def test_invert_signal():
    assert invert_signal("LONG") == "SHORT"
    assert invert_signal("SHORT") == "LONG"


def test_dollar_per_unit_usd_quote_uses_contract_size():
    # EURUSD ends in USD and has a contract size → k = contract_size
    k = dollar_per_unit("EURUSD", contract_size=100000.0,
                         tick_size=0.00001, tick_value=1.0)
    assert k == 100000.0


def test_dollar_per_unit_usd_base_uses_tick_ratio():
    # USDJPY does NOT end in USD → k = tick_value / tick_size
    k = dollar_per_unit("USDJPY", contract_size=100000.0,
                         tick_size=0.001, tick_value=0.65)
    assert k == 0.65 / 0.001


def test_dollar_per_unit_zero_contract_falls_back_to_tick_ratio():
    k = dollar_per_unit("EURUSD", contract_size=0.0,
                        tick_size=0.00001, tick_value=1.0)
    assert k == 1.0 / 0.00001
