"""Exercise the actual NG cases and Pick_Part prefix without a controller.

Only the listed SPEL+ statements are supported. Motion, time, and network writes
are recorded, not executed. This does not simulate CP interpolation or RC+ timing.
"""

import re
import unittest
from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / "VT6" / "Main.prg").read_text(
    encoding="utf-8"
)
FUNCTIONS = {
    match[1]: match[0]
    for match in re.finditer(
        r"^Function (\w+)[^\n]*\n.*?^Fend\b", SOURCE, re.M | re.S
    )
}
NG_BIT = "RpiNgStopReq"


def statements(source):
    return [
        line for raw in source.splitlines()
        if (line := raw.split("'", 1)[0].strip())
    ]


def response_case(response):
    match = re.search(
        rf'^\s*Case "{re.escape(response)}"\s*\n(.*?)'
        r'(?=^\s*(?:Case "|Default\b))',
        FUNCTIONS["RpiNet"], re.M | re.S,
    )
    if not match:
        raise AssertionError(f"Missing response case: {response}")
    return statements(match[1])


def checkpoint():
    prefix = FUNCTIONS["Pick_Part"].split(
        "\tIf MemSw(IsPickOk) = False Then", 1
    )[0]
    return statements(prefix)[1:]  # Exclude the Function declaration.


class TasksQuit(Exception):
    pass


class Rig:
    def __init__(self, ng=False, network_failure=False):
        self.bits = {NG_BIT: ng}
        self.network_failure = network_failure
        self.events = []

    def execute(self, lines):
        active = [True]
        for line in lines:
            if match := re.fullmatch(r"If MemSw\((\w+)\) = On Then", line):
                active.append(active[-1] and self.bits.get(match[1], False))
                continue
            if line == "EndIf":
                if len(active) == 1:
                    raise AssertionError("Unexpected EndIf")
                active.pop()
                continue
            if not active[-1]:
                continue
            if match := re.fullmatch(r"(MemOn|MemOff) (\w+)", line):
                value = match[1] == "MemOn"
                self.bits[match[2]] = value
                self.events.append(("memory", match[2], value))
            elif match := re.fullmatch(r'Print #202, "([A-Z_]+)"', line):
                self.events.append(("send", match[1]))
                if self.network_failure:
                    raise ConnectionError("Simulated Print #202 failure")
            elif match := re.fullmatch(r'Print "([A-Z ]+)"', line):
                self.events.append(("console", match[1]))
            elif match := re.fullmatch(r"Wait ([0-9.]+)", line):
                self.events.append(("wait", float(match[1])))
            elif line in {
                "Move P_Pick_Part +Z(125) +X(33.5) CP",
                "Move P_Pick_Part1",
            }:
                self.events.append(("motion", line))
            elif line == "Quit All":
                self.events.append(("quit",))
                raise TasksQuit
            else:
                raise AssertionError(f"Unsupported SPEL+ statement: {line}")
        if len(active) != 1:
            raise AssertionError("Unclosed If")


class CameraNgStopTests(unittest.TestCase):
    def test_all_ng_cases_latch_before_logging_without_quitting(self):
        for kind in ("INNER", "GLUE", "NP"):
            with self.subTest(kind=kind):
                rig = Rig()
                rig.execute(response_case(f"{kind},NG"))
                self.assertEqual(rig.events, [
                    ("memory", NG_BIT, True), ("send", f"NO_{kind}"),
                ])

    def test_ok_does_not_clear_an_existing_ng(self):
        rig = Rig(ng=True)
        for kind in ("INNER", "GLUE", "NP"):
            rig.execute(response_case(f"{kind},OK"))
            self.assertTrue(rig.bits[NG_BIT])

    def test_failed_error_send_cannot_prevent_later_checkpoint_stop(self):
        for kind in ("INNER", "GLUE", "NP"):
            with self.subTest(kind=kind):
                rig = Rig(network_failure=True)
                with self.assertRaises(ConnectionError):
                    rig.execute(response_case(f"{kind},NG"))
                self.assertTrue(rig.bits[NG_BIT])
                with self.assertRaises(TasksQuit):
                    rig.execute(checkpoint())

    def test_normal_checkpoint_has_no_added_wait_or_motion(self):
        rig = Rig()
        rig.execute(checkpoint())
        self.assertEqual(rig.events, [
            ("memory", "RpiCalibReq", True),
            ("motion", "Move P_Pick_Part +Z(125) +X(33.5) CP"),
        ])

    def test_ng_checkpoint_runs_exact_requested_stop_sequence(self):
        rig = Rig(ng=True)
        with self.assertRaises(TasksQuit):
            rig.execute(checkpoint())
        self.assertEqual(rig.events, [
            ("memory", "RpiCalibReq", True),
            ("motion", "Move P_Pick_Part +Z(125) +X(33.5) CP"),
            ("wait", 0.5),
            ("motion", "Move P_Pick_Part1"),
            ("wait", 1.0),
            ("quit",),
        ])
        self.assertTrue(rig.bits[NG_BIT])

    def test_late_ng_is_held_until_next_checkpoint(self):
        rig = Rig()
        rig.execute(checkpoint())
        rig.execute(response_case("GLUE,NG"))
        self.assertNotIn(("quit",), rig.events)
        rig.execute(response_case("INNER,OK"))
        with self.assertRaises(TasksQuit):
            rig.execute(checkpoint())

    def test_duplicate_ng_does_not_reset_the_latch(self):
        rig = Rig()
        for response in ("INNER,NG", "INNER,NG", "GLUE,NG", "NP,OK"):
            rig.execute(response_case(response))
        self.assertTrue(rig.bits[NG_BIT])
        self.assertNotIn(("quit",), rig.events)

    def test_network_task_does_not_move_quit_or_clear_ng(self):
        lines = statements(FUNCTIONS["RpiNet"])
        self.assertNotIn("Quit All", lines)
        self.assertNotIn(f"MemOff {NG_BIT}", lines)
        self.assertFalse(any(re.match(r"(?:Move|Go|Pass|Jump)\b", line) for line in lines))

    def test_only_init_clears_ng_before_starting_network_task(self):
        self.assertEqual(statements(SOURCE).count(f"MemOff {NG_BIT}"), 1)
        init = statements(FUNCTIONS["Init"])
        self.assertLess(init.index(f"MemOff {NG_BIT}"), init.index("Xqt RpiNet"))

    def test_only_one_checkpoint_and_other_faults_still_quit(self):
        self.assertEqual(
            statements(SOURCE).count(f"If MemSw({NG_BIT}) = On Then"), 1
        )
        self.assertIn("Quit All", statements(FUNCTIONS["FatalError"]))


if __name__ == "__main__":
    unittest.main()
