import unittest
import json
from src.utils.template import transform_template

class TestTemplateEscaping(unittest.TestCase):
    def test_json_escaping_accents(self):
        # A snippet containing Czech accents
        snippet = "Sleduj indicie a vyhraj nový iPhone 17 Pro!"
        context = {
            "root": {
                "data": {
                    "snippet": snippet
                }
            }
        }
        
        # We test $root.data interpolation which uses json.dumps
        # Currently, it is expected to escape 'ý' to '\u00fd'
        template = "$root.data"
        result = transform_template(template, context)
        
        # If it escapes, it will contain \u00fd
        # If it doesn't escape, it will contain 'ý'
        
        print(f"Result for $root.data: {result}")
        
        # The goal is that it SHOULD contain 'ý', but currently it likely contains \u00fd
        # We'll assert that it contains 'ý' to make it fail if it's escaping
        self.assertIn("ý", result, f"Expected 'ý' in result, but got: {result}")

    def test_string_interpolation_json_escaping(self):
        snippet = "Dokážeš složit celý kód?"
        context = {
            "root": {
                "subject": snippet
            }
        }
        template = "Message: $root.subject"
        result = transform_template(template, context)
        
        print(f"Result for string interpolation: {result}")
        
        # 'á', 'ž', 'š', 'í', 'ý', 'ó' etc should not be escaped
        self.assertIn("á", result)
        self.assertIn("ž", result)
        self.assertIn("š", result)

if __name__ == "__main__":
    unittest.main()
