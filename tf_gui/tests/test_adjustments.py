import os
import sys
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from PyQt5 import QtWidgets  # noqa: E402

from main import (  # noqa: E402
    AdjustmentDialog,
    AdjustmentStore,
    MAX_VALUE,
    MIN_VALUE,
    STEP,
)


class AdjustmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_dialog_clamps_values_and_disables_limit_buttons(self) -> None:
        dialog = AdjustmentDialog(
            "PickNP", {"X": 0.0, "Y": 0.0, "Z": 0.0, "U": 0.0}
        )

        for _ in range(20):
            dialog.adjust_axis("X", STEP)
        self.assertEqual(dialog.values()["X"], MAX_VALUE)
        self.assertFalse(dialog.btnXPlus.isEnabled())

        for _ in range(40):
            dialog.adjust_axis("X", -STEP)
        self.assertEqual(dialog.values()["X"], MIN_VALUE)
        self.assertFalse(dialog.btnXMinus.isEnabled())

    def test_three_stations_are_saved_and_loaded_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "adjustments.json"
            store = AdjustmentStore(path)
            values = store.load()
            values["PickNP"]["X"] = 0.15
            values["PickNPS"]["Y"] = -0.25
            values["DropNP"]["U"] = 0.50
            store.save(values)

            loaded = AdjustmentStore(path).load()
            self.assertEqual(loaded["PickNP"]["X"], 0.15)
            self.assertEqual(loaded["PickNPS"]["Y"], -0.25)
            self.assertEqual(loaded["DropNP"]["U"], 0.50)


if __name__ == "__main__":
    unittest.main()
