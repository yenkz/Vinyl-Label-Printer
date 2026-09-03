import importlib
import unittest

from vinyl_labels import cli, db
from vinyl_labels.paths import PROJECT_ROOT, TEMPLATES_DIR


class ProjectStructureTests(unittest.TestCase):
    def test_root_contains_no_python_modules(self):
        self.assertEqual(list(PROJECT_ROOT.glob("*.py")), [])

    def test_every_cli_command_targets_an_importable_command_module(self):
        for module_name in cli.COMMANDS.values():
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                self.assertTrue(callable(module.main))

    def test_mutable_data_stays_outside_the_source_package(self):
        self.assertEqual(db.DB_PATH, PROJECT_ROOT / "vinyl_labels.db")
        self.assertTrue((TEMPLATES_DIR / "editor.html").is_file())


if __name__ == "__main__":
    unittest.main()
