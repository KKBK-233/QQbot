"""使用本地RSA签名验证时间容差，同时检查身份与签名校验仍然生效。"""

import json
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

import auth


class IdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk["kid"] = "test-key"

    def claims(self):
        now = int(time.time())
        return {"iss": auth.ISSUER, "aud": "test-client", "sub": "test-subject",
                "iat": now, "exp": now + 3600, "nonce": "test-nonce"}

    def validate(self, claims, key=None, nonce="test-nonce"):
        token = jwt.encode(claims, key or self.key, algorithm="RS256", headers={"kid": "test-key"})
        with patch.object(auth.httpx, "Client") as client:
            response = client.return_value.__enter__.return_value.get.return_value
            response.json.return_value = {"keys": [self.jwk]}
            return auth.validate_identity(token, "test-client", nonce)

    def test_small_future_iat_and_nbf_are_allowed(self):
        claims = self.claims()
        claims["iat"] += 5
        claims["nbf"] = claims["iat"]
        self.assertEqual(self.validate(claims)["sub"], "test-subject")

    def test_large_future_iat_is_rejected(self):
        claims = self.claims()
        claims["iat"] += 120
        with self.assertRaises(jwt.ImmatureSignatureError):
            self.validate(claims)

    def test_large_future_nbf_is_rejected(self):
        claims = self.claims()
        claims["nbf"] = int(time.time()) + 120
        with self.assertRaises(jwt.ImmatureSignatureError):
            self.validate(claims)

    def test_expired_token_is_rejected(self):
        claims = self.claims()
        claims.update(iat=int(time.time()) - 3600, exp=int(time.time()) - 120)
        with self.assertRaises(jwt.ExpiredSignatureError):
            self.validate(claims)

    def test_wrong_signature_is_rejected(self):
        with self.assertRaises(jwt.InvalidSignatureError):
            self.validate(self.claims(), key=self.other_key)

    def test_wrong_issuer_and_audience_are_rejected(self):
        for field, error in (("iss", jwt.InvalidIssuerError), ("aud", jwt.InvalidAudienceError)):
            with self.subTest(field=field):
                claims = self.claims()
                claims[field] = "wrong"
                with self.assertRaises(error):
                    self.validate(claims)

    def test_wrong_nonce_is_rejected(self):
        with self.assertRaises(auth.AuthError):
            self.validate(self.claims(), nonce="wrong")


if __name__ == "__main__":
    unittest.main()
