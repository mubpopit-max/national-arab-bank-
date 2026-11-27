"""Compile embedded Jinja templates without importing the production app."""
import ast
import unittest
from pathlib import Path

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
ENV = Environment()
TEMPLATE_CONSTANTS = {
    "LOGIN_HTML",
    "REGISTER_HTML",
    "FUNDING_HTML",
    "FOUNDER_PANEL_HTML",
    "shell",
}


class EmbeddedTemplateSyntaxTests(unittest.TestCase):
    def test_embedded_templates_compile(self):
        checked = 0
        failures = []
        for filename in ("app.py", "empire_store.py"):
            source = (ROOT / filename).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=filename)
            candidates = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    names = {
                        target.id for target in node.targets
                        if isinstance(target, ast.Name)
                    }
                    if names & TEMPLATE_CONSTANTS and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        candidates.append((node.lineno, node.value.value))
                if isinstance(node, ast.Call):
                    function = node.func
                    is_renderer = (
                        isinstance(function, ast.Name) and function.id == "render_template_string"
                    ) or (
                        isinstance(function, ast.Attribute) and function.attr == "render_template_string"
                    )
                    if is_renderer and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        candidates.append((node.lineno, node.args[0].value))
            for line, template in candidates:
                if "{{" not in template and "{%" not in template:
                    continue
                checked += 1
                try:
                    ENV.from_string(template)
                except Exception as exc:
                    failures.append(f"{filename}:{line}: {exc}")
        self.assertGreater(checked, 0, "No embedded templates were checked")
        self.assertEqual([], failures, "\\n".join(failures))


if __name__ == "__main__":
    unittest.main()
