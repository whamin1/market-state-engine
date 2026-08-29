import os
import unittest
from unittest.mock import patch

from market_state_engine.prediction_telegram import send_prediction_alert


class PredictionTelegramTests(unittest.TestCase):
    @patch("market_state_engine.prediction_telegram._post_message", return_value=(200, "ok"))
    def test_sends_with_the_telegram5_token(self, post_message):

        sent = send_prediction_alert("research alert", token="token-five", chat_id="chat-id")

        self.assertTrue(sent)
        post_message.assert_called_once_with(
            "https://api.telegram.org/bottoken-five/sendMessage",
            {"chat_id": "chat-id", "text": "research alert"},
        )

    @patch("market_state_engine.prediction_telegram._post_message")
    def test_missing_configuration_does_not_send(self, post_message):
        with patch.dict(os.environ, {}, clear=True):
            sent = send_prediction_alert("research alert", env_path="does-not-exist.env")

        self.assertFalse(sent)
        post_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
