"""Offline tests of the actual layer-related SPEL+ source, without robot I/O.

This deliberately small translator supports only the statements in these
functions. It is NOT an Epson compiler or controller/timing simulation.
Unknown statements fail rather than silently being skipped. Search_pallet is
executed only up to its existing ejecting branch (no motion is executed).
"""

import re
import unittest
from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / "VT6" / "Main.prg").read_text()
FUNCTIONS = {
    match[1]: match[0]
    for match in re.finditer(
        r"^Function (\w+)[^\n]*\n.*?^Fend\b", SOURCE, re.M | re.S
    )
}


class Ref:
    def __init__(self, value=0):
        self.value = value


def expression(text):
    text = text.replace("ByRef confirmedLayer", "REFERENCE_ARGUMENT")
    text = re.sub(r"\bconfirmedLayer\b", "confirmedLayer.value", text)
    text = text.replace("REFERENCE_ARGUMENT", "confirmedLayer")
    text = re.sub(r"\bReadPlateLayer\b(?!\s*\()", "ReadPlateLayer()", text)
    text = text.replace("<>", "!=")
    text = re.sub(r"(?<![<>=!])=(?!=)", "==", text)
    return re.sub(r"\bAnd\b", "&", text)


def translate(source):
    lines = [line.split("'", 1)[0].strip() for line in source.splitlines()]
    header = re.fullmatch(
        r"Function (\w+)(?:\((.*?)\))?(?: As \w+)?", lines[0]
    )
    if not header:
        raise AssertionError(lines[0])
    name, args = header.groups()
    params = []
    for param in args.split(",") if args else []:
        params.append(re.fullmatch(r"(?:ByRef )?(\w+) As \w+", param.strip())[1])
    output = [f"def {name}({', '.join(params)}):", "    global Layer, Count", "    _result = None"]
    indent = 1
    for line in lines[1:]:
        if not line:
            continue
        if line in {"EndIf", "Loop"}:
            indent -= 1
            continue
        if line.startswith("ElseIf ") or line == "Else":
            indent -= 1
        statement = None
        if line == "Fend" or line == "Exit Function":
            statement = "return _result"
        elif line.startswith("Integer "):
            names = line.removeprefix("Integer ").split(",")
            statement = "; ".join(
                f"{n.strip()} = {'Ref()' if n.strip() == 'confirmedLayer' else '0'}"
                for n in names
            )
        elif line.startswith("If ") and line.endswith(" Then"):
            statement = f"if {expression(line[3:-5])}:"
        elif line.startswith("ElseIf ") and line.endswith(" Then"):
            statement = f"elif {expression(line[7:-5])}:"
        elif line == "Else":
            statement = "else:"
        elif line == "Do":
            statement = "while True:"
        elif line.startswith("Do While "):
            statement = f"while {expression(line[9:])}:"
        elif line.startswith("Call "):
            statement = expression(line[5:])
        elif line.startswith("Print "):
            statement = "pass"  # Console only; no I/O or control side effects.
        elif re.match(r"^(Wait|TmReset) ", line):
            command, arg = line.split(" ", 1)
            statement = f"{command}({expression(arg)})"
        elif re.match(r"^(On|Off|MemOff) ", line):
            command, label = line.split(" ", 1)
            statement = f"write_bit({label!r}, {command == 'On'})"
        elif match := re.fullmatch(r"(\w+)\s*=\s*(.*)", line):
            target, value = match.groups()
            target = "_result" if target == name else target
            if target == "confirmedLayer":
                target += ".value"
            statement = f"{target} = {expression(value)}"
        if statement is None:
            raise AssertionError(f"Unsupported SPEL+ statement: {line}")
        output.append("    " * indent + statement)
        if statement.endswith(":"):
            indent += 1
    if indent != 1:
        raise AssertionError(f"Unbalanced block in {name}")
    return "\n".join(output)


class SignalRig:
    def __init__(self, layer=2, count=14, schedule=((0, 2),), limit_ms=5000):
        self.now_ms = 0
        self.schedule = schedule
        self.limit_ms = limit_ms
        self.timers = {}
        self.writes = []
        self.read_count = 0
        self.env = {
            "Layer": layer, "Count": count, "Ref": Ref, "On": True,
            "Sw": self.read_bit, "Wait": self.wait,
            "TmReset": lambda number: self.timers.update({number: self.now_ms}),
            "Tmr": lambda number: (self.now_ms - self.timers[number]) / 1000,
            "write_bit": lambda label, value: self.writes.append((self.now_ms, label, value)),
        }
        for bit in range(3):
            self.env[f"plateBit{bit}"] = bit
        for name, value in re.findall(r"^#define (LAYER_\w+) ([\d.]+)$", SOURCE, re.M):
            self.env[name] = float(value) if "." in value else int(value)
        for name in ("ReadPlateLayer", "IsLayerChanged", "UpdateLayer"):
            exec(translate(FUNCTIONS[name]), self.env)
        search_prefix = FUNCTIONS["Search_pallet"].split("\tIf (Sw(ejecting)", 1)[0]
        exec(translate(search_prefix + "Fend"), self.env)

    def read_bit(self, bit):
        self.read_count += 1
        code = next(code for at, code in reversed(self.schedule) if at <= self.now_ms)
        return bool(code & (1 << bit))

    def wait(self, seconds):
        self.now_ms += round(seconds * 1000)
        if self.now_ms > self.limit_ms:
            raise TimeoutError("Offline test stopped an unending signal trace")

    def search(self):
        self.env["Search_pallet"]()

    def echoes(self):
        return [(at, bit, value) for at, bit, value in self.writes if bit.startswith("plateConfirm")]


class LayerDebounceTests(unittest.TestCase):
    def test_settings(self):
        rig = SignalRig()
        self.assertEqual(rig.env["LAYER_STABLE_SECONDS"], 0.5)
        self.assertEqual(rig.env["LAYER_SAMPLE_SECONDS"], 0.01)
        self.assertEqual(rig.env["LAYER_DEBOUNCE_TIMER"], 5)

    def test_same_layer_has_no_wait_or_count_reset(self):
        rig = SignalRig()
        rig.search()
        self.assertEqual((rig.now_ms, rig.env["Layer"], rig.env["Count"]), (0, 2, 14))
        self.assertEqual(rig.echoes(), [])

    def test_short_bounce_back_does_not_reset_count(self):
        for duration in (10, 100, 490, 500):
            with self.subTest(duration=duration):
                rig = SignalRig(schedule=((0, 3), (duration, 2)))
                rig.search()
                self.assertEqual(rig.env["Count"], 14)
                self.assertEqual(rig.env["Layer"], 2)
                self.assertEqual(rig.echoes(), [])

    def test_stable_change_is_accepted_at_500ms_once(self):
        rig = SignalRig(schedule=((0, 3),))
        rig.search()
        self.assertEqual((rig.now_ms, rig.env["Layer"], rig.env["Count"]), (500, 3, 1))
        self.assertEqual(len(rig.echoes()), 3)
        rig.env["Count"] = 8
        rig.search()
        self.assertEqual(rig.env["Count"], 8)
        self.assertEqual(len(rig.echoes()), 3)

    def test_each_different_candidate_restarts_full_timer(self):
        rig = SignalRig(schedule=((0, 3), (300, 4), (600, 5)))
        rig.search()
        self.assertEqual((rig.now_ms, rig.env["Layer"], rig.env["Count"]), (1100, 5, 1))
        self.assertTrue(all(at == 1100 for at, _, _ in rig.echoes()))

    def test_change_on_threshold_is_not_accepted_as_previous_code(self):
        rig = SignalRig(schedule=((0, 3), (500, 4)))
        rig.search()
        self.assertEqual((rig.now_ms, rig.env["Layer"]), (1000, 4))

    def test_continuous_different_codes_never_reset_count_or_echo(self):
        schedule = tuple((t, 3 if t % 400 == 0 else 4) for t in range(0, 2200, 200))
        rig = SignalRig(schedule=schedule, limit_ms=2000)
        with self.assertRaises(TimeoutError):
            rig.search()
        self.assertEqual((rig.env["Layer"], rig.env["Count"]), (2, 14))
        self.assertEqual(rig.echoes(), [])
        self.assertIn((0, "IsPalletOk", False), rig.writes)

    def test_repeated_short_bounces_across_calls_do_not_accumulate(self):
        rig = SignalRig(schedule=((0, 3), (400, 2), (500, 3), (900, 2)))
        rig.search()
        rig.now_ms = 500
        rig.search()
        self.assertEqual((rig.env["Layer"], rig.env["Count"]), (2, 14))
        self.assertEqual(rig.echoes(), [])

    def test_echo_uses_confirmed_snapshot_without_input_reread(self):
        rig = SignalRig(schedule=((0, 3), (510, 7)))
        confirmed = Ref()
        self.assertTrue(rig.env["IsLayerChanged"](confirmed))
        self.assertEqual(rig.env["Count"], 14)
        self.assertEqual(rig.env["Layer"], 2)
        self.assertEqual(rig.echoes(), [])
        reads = rig.read_count
        rig.now_ms = 510
        rig.env["UpdateLayer"](confirmed.value)
        self.assertEqual(rig.read_count, reads)
        self.assertEqual(rig.env["Layer"], 3)
        self.assertEqual([value for _, _, value in rig.echoes()], [True, True, False])

    def test_all_three_bit_codes_read_and_echo_without_range_policy(self):
        for code in range(8):
            with self.subTest(code=code):
                rig = SignalRig(layer=(code + 1) % 8, schedule=((0, code),))
                rig.search()
                self.assertEqual(rig.env["Layer"], code)
                self.assertEqual(rig.env["Count"], 1)
                self.assertEqual([v for _, _, v in rig.echoes()], [bool(code & (1 << b)) for b in range(3)])

    def test_all_update_calls_supply_snapshot(self):
        calls = [line.strip() for line in SOURCE.splitlines() if "UpdateLayer" in line and not line.startswith("Function ")]
        self.assertEqual(calls.count("Call UpdateLayer(ReadPlateLayer)"), 4)
        self.assertEqual(calls.count("Call UpdateLayer(confirmedLayer)"), 1)
        self.assertEqual(len(calls), 5)

    def test_timer_does_not_reuse_existing_timers(self):
        direct_timers = re.findall(r"\b(?:Tmr|TmReset)\s*\(?\s*(\d+)", SOURCE)
        self.assertNotIn("5", direct_timers)


if __name__ == "__main__":
    unittest.main()
