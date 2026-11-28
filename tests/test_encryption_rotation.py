import os
import unittest
from unittest.mock import patch

from flask import Flask
from cryptography.fernet import Fernet

import empire_store


class EncryptionKeyRotationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "stable-test-flask-secret"

    def test_dedicated_key_reads_new_and_legacy_ciphertext(self):
        with self.app.app_context():
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("EMPIRE_DATA_ENCRYPTION_KEY", None)
                legacy_cipher = empire_store.encryption()
                legacy_token = legacy_cipher.encrypt(b"legacy-secret")

                dedicated_key = Fernet.generate_key().decode("ascii")
                os.environ["EMPIRE_DATA_ENCRYPTION_KEY"] = dedicated_key
                active_cipher = empire_store.encryption()
                current_token = active_cipher.encrypt(b"current-secret")

                self.assertEqual(
                    empire_store.decrypt_encrypted(legacy_token),
                    b"legacy-secret",
                )
                self.assertEqual(
                    empire_store.decrypt_encrypted(current_token),
                    b"current-secret",
                )

    def test_legacy_mode_remains_compatible_when_no_dedicated_key(self):
        with self.app.app_context():
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("EMPIRE_DATA_ENCRYPTION_KEY", None)
                token = empire_store.encryption().encrypt(b"still-readable")
                self.assertEqual(
                    empire_store.decrypt_encrypted(token),
                    b"still-readable",
                )


if __name__ == "__main__":
    unittest.main()
