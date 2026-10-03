import unittest

from src.config import RESPONSE_PROVIDER


class ResponseProviderTests(unittest.TestCase):
    def test_response_provider_is_supported(self):
        self.assertIn(RESPONSE_PROVIDER, {"google", "openrouter"})
