import datetime as dt

from lockwire import checks, pins
from lockwire.schema import (
    Connector,
    Contract,
    DeviceInstance,
    InputVoltageRange,
    Link,
    ModelRecord,
    Pin,
    Provenance,
    RobotInfo,
    Topology,
    Wire,
    WireEnd,
)


def dev(id, model, conn, *pin_list, **kw):
    return DeviceInstance(
        id=id, model=model, owner="t", connectors=[Connector(id=conn, pins=list(pin_list))], **kw
    )


def wire(a, b, wid="w1"):
    ends = [WireEnd(device=d, connector=c, pin=p) for d, c, p in (a, b)]
    return Wire(id=wid, awg=26, installation="free_air", owner="t", from_=ends[0], to=ends[1])


def contract(devices, *wires):
    prov = Provenance(witness="human", source="x", locked_at=dt.date(2026, 1, 1))
    model = ModelRecord(
        source="x", source_sha256="0", structure_hash="0", links=[Link(name="b")], provenance=prov
    )
    return Contract(
        robot=RobotInfo(name="t"),
        model=model,
        devices=devices,
        topology=Topology(wires=list(wires)),
    )


def uno(*extra):
    return dev("uno", "arduino-uno-r3", "JDIGITAL", Pin(number="D1", voltage_v=5.0), *extra)


def pi_to_uno():
    pi = dev("pi", "raspberry-pi-4b", "J8", Pin(number=8, name="GPIO14", voltage_v=3.3))
    return contract([pi, uno()], wire(("pi", "J8", 8), ("uno", "JDIGITAL", "D1")))


def test_pi_gpio_to_arduino_tx_is_flagged_as_advice():
    found = [f for f in pins.check_voltage(pi_to_uno()) if "drives 5 V" in f.observed]
    assert [f.observed for f in found] == ["GPIO14 is 3.3 V; Arduino D1 drives 5 V"]
    assert found[0].advice_unsourced  # the Pi card carries no 5 V tolerance number


def test_esp32_input_over_card_limit_is_a_sourced_warning():
    esp = dev("esp", "esp32-devkitc-wroom-32", "J2", Pin(number=5, voltage_v=3.3))
    (f,) = pins.check_voltage(
        contract([esp, uno()], wire(("esp", "J2", 5), ("uno", "JDIGITAL", "D1")))
    )
    assert (f.severity, f.advice_unsourced, "espressif" in f.source.lower()) == (
        "warning",
        False,
        True,
    )


def test_low_level_over_card_vil_is_sourced_from_the_receiver_card():
    found = [f for f in pins.check_voltage(pi_to_uno()) if "low" in f.observed]
    assert len(found) == 1 and not found[0].advice_unsourced and "raspberrypi" in found[0].source


def two(voh, vih):
    a = dev("a", "none", "J", Pin(number=1, direction="out", voh=voh))
    b = dev("b", "none", "J", Pin(number=1, direction="in", vih=vih))
    return contract([a, b], wire(("a", "J", 1), ("b", "J", 1)))


def test_voh_below_vih_is_flagged_and_enough_is_not():
    (f,) = pins.check_voltage(two(2.4, 3.0))
    assert "drives only 2.4 V" in f.observed and f.advice_unsourced
    assert pins.check_voltage(two(3.3, 2.0)) == []


def test_output_to_output_only_when_both_drive():
    a = dev("a", "none", "J", Pin(number=1, direction="out"))
    for other, n in (("out", 1), ("in", 0)):
        b = dev("b", "none", "J", Pin(number=1, direction=other))
        c = contract([a, b], wire(("a", "J", 1), ("b", "J", 1)))
        assert len(pins.check_output_clash(c)) == n


def test_output_clash_is_per_net_through_two_wires():
    a, c = (dev(i, "none", "J", Pin(number=1, direction="out")) for i in "ac")
    b = dev("b", "none", "J", Pin(number=1, direction="in"))
    w = [wire(("a", "J", 1), ("b", "J", 1), "w1"), wire(("c", "J", 1), ("b", "J", 1), "w2")]
    (f,) = pins.check_output_clash(contract([a, b, c], *w))
    assert "a 1" in f.observed and "c 1" in f.observed and f.advice_unsourced
    assert pins.check_output_clash(contract([a, b, c], w[0])) == []


def test_strapping_pin_wired_is_a_warning_with_the_cards_reason_and_source():
    esp = dev("esp", "esp32-devkitc-wroom-32", "J3", Pin(number=14))
    peer = dev("p", "none", "J", Pin(number=1))
    (f,) = pins.check_special_pins(
        contract([esp, peer], wire(("esp", "J3", "GPIO0"), ("p", "J", 1)))
    )
    card = pins._cards()["esp32-devkitc-wroom-32"]
    assert "strapping pin" in f.observed and f.source == card.connectors[1].source
    assert not f.advice_unsourced and f.severity == "warning"


def loaded(n, ma):
    return dev(
        "uno",
        "arduino-uno-r3",
        "JDIGITAL",
        *[Pin(number=i, max_current_ma=ma) for i in range(2, 2 + n)],
    )


def test_board_total_over_card_limit_with_every_pin_in_limit():
    (f,) = pins.check_current(contract([loaded(11, 19)]))
    assert f.quantity == "topology/wires/uno" and "209 mA" in f.observed and not f.advice_unsourced
    assert pins.check_current(contract([loaded(3, 19)])) == []


def test_single_pin_over_card_limit():
    (f,) = pins.check_current(contract([loaded(1, 25)]))
    assert "25 mA" in f.observed and "per pin" in f.expected


def test_special_pins_reserved_input_only_and_adc2_with_radio():
    esp = dev(
        "esp",
        "esp32-devkitc-wroom-32",
        "J2",
        Pin(number=13),
        Pin(number=5, direction="out"),
        Pin(number=9),
        radio_on=True,
    )
    peer = dev("p", "none", "J", *[Pin(number=i) for i in (1, 2, 3)])
    w = [wire(("esp", "J2", n), ("p", "J", i), f"w{n}") for i, n in enumerate((13, 5, 9), 1)]
    got = pins.check_special_pins(contract([esp, peer], *w))
    pick = lambda word: [f for f in got if word in f.observed]  # noqa: E731
    (res,), (inp,), adc = pick("reserved"), pick("input-only"), pick("ADC2")
    assert res.quantity.endswith("w13") and not res.advice_unsourced
    assert inp.quantity.endswith("w5") and len(adc) == 2 and all(f.advice_unsourced for f in adc)


def test_missing_input_emits_nothing():
    bare = [
        dev("a", "none", "J", Pin(number=1, direction="out")),
        dev("b", "none", "J", Pin(number=1)),
    ]
    c = contract(bare, wire(("a", "J", 1), ("b", "J", 1)))
    assert [f for fn in pins.ENGINE_CHECKS for f in fn(c)] == []
    assert pins.check_voltage(contract([], wire(("x", None, 1), ("y", None, 1)))) == []


def test_erc_report_rows_and_registry():
    (row, *_) = pins.erc_report(pi_to_uno())
    assert set(row) == {"where", "problem", "limit", "basis"}
    assert pins.check_voltage in list(checks._engine_checks())


def power_to_ground():
    reg = dev("reg", "x", "J1", Pin(number=1, type="power_out", voltage_v=5.0))
    mcu = dev("mcu", "x", "J2", Pin(number=1, type="power_in", voltage_v=0.0))
    return contract([reg, mcu], wire(("reg", "J1", 1), ("mcu", "J2", 1)))


def test_page_pin_facts_match_the_rule_that_flags_a_power_to_ground_wire():
    """The numbers the panel's live rule reads are the numbers check_voltage flags on."""
    from lockwire import page

    c = power_to_ground()
    assert [f.observed for f in pins.check_voltage(c)] == ["1 is 0 V; reg 1 drives 5 V"]
    devices = page._wiring(c)["wiring_devices"]
    live = page._wiring_live(c, devices)
    by = {f["key"]: f for f in live["wiring_pins"]}
    assert by["reg|J1|1"]["level_v"] == 5.0 and by["reg|J1|1"]["drives"]
    assert by["mcu|J2|1"]["receives"] and by["mcu|J2|1"]["limit_v"] == pins._MARGIN_V
    assert [(f["wire"], f["problem"]) for f in live["wiring_findings"]] == [
        ("w1", "1 is 0 V; reg 1 drives 5 V")
    ]


def esp_rx(pin, rail=12.0):
    esp = dev("esp", "esp32-devkitc-wroom-32", "J2", pin)
    drv = dev("d", "none", "J", Pin(number=1, direction="out", voltage_v=rail))
    return contract([esp, drv], wire(("d", "J", 1), ("esp", "J2", pin.number)))


def test_5v_output_into_esp32_5v_pin_is_not_flagged():
    assert pins.check_voltage(esp_rx(Pin(number=19), 5.0)) == []


def test_5v_output_into_esp32_gpio_is_flagged_against_the_card_limit():
    (f,) = pins.check_voltage(esp_rx(Pin(number=5, voltage_v=3.3), 5.0))
    assert "<= 3.6 V" in f.expected and "espressif" in f.source.lower()


def test_power_in_pin_is_judged_by_its_own_declared_range_max():
    rng = lambda hi: InputVoltageRange(  # noqa: E731
        min={"value": 4.5, "unit": "V"}, max={"value": hi, "unit": "V"}
    )
    assert pins.check_voltage(esp_rx(Pin(number=19, voltage_range=rng(24)))) == []
    (f,) = pins.check_voltage(esp_rx(Pin(number=19, voltage_range=rng(6))))
    assert "<= 6 V" in f.expected
