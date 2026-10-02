"""Meshenger-compatible signaling crypto.

Mirrors Crypto.kt from meshenger-android:
  encrypt: plaintext -> crypto_sign(own_priv) -> prepend own_pub_sign
           -> crypto_box_seal(recipient_pub_sign converted ed25519->curve25519)
  decrypt: crypto_box_seal_open(own keys) -> split sender_pub_sign
           -> crypto_sign_open(sender_pub_sign)

Framing (mirrors PacketReader/PacketWriter):
  4-byte big-endian length header + payload.
"""

import asyncio
import struct

from nacl.bindings import (
    crypto_box_seal,
    crypto_box_seal_open,
    crypto_sign_ed25519_pk_to_curve25519,
    crypto_sign_ed25519_sk_to_curve25519,
)
from nacl.exceptions import CryptoError
from nacl.signing import SigningKey, VerifyKey

SIGN_PUB_LEN = 32


def public_key_from_seed(seed: bytes) -> bytes:
    return bytes(SigningKey(seed).verify_key)


def _box_keypair(seed: bytes):
    sk = SigningKey(seed)
    sk64 = bytes(sk) + bytes(sk.verify_key)  # libsodium secret key format
    pub_box = crypto_sign_ed25519_pk_to_curve25519(bytes(sk.verify_key))
    priv_box = crypto_sign_ed25519_sk_to_curve25519(sk64)
    return pub_box, priv_box


def encrypt_message(plaintext: bytes, recipient_pub_sign: bytes, own_seed: bytes) -> bytes:
    """Seal a signed message for the recipient. Raises on bad inputs."""
    if len(recipient_pub_sign) != SIGN_PUB_LEN:
        raise ValueError("bad recipient public key length")
    sk = SigningKey(own_seed)
    own_pub_sign = bytes(sk.verify_key)
    signed = sk.sign(plaintext)  # signature || plaintext
    data = own_pub_sign + bytes(signed)
    recipient_pub_box = crypto_sign_ed25519_pk_to_curve25519(recipient_pub_sign)
    return crypto_box_seal(data, recipient_pub_box)


def decrypt_message(ciphertext: bytes, own_seed: bytes):
    """Open a sealed message. Returns (sender_pub_sign, plaintext). Raises CryptoError."""
    sk = SigningKey(own_seed)
    own_pub_sign = bytes(sk.verify_key)
    pub_box, priv_box = _box_keypair(own_seed)
    data = crypto_box_seal_open(ciphertext, pub_box, priv_box)
    if len(data) <= SIGN_PUB_LEN:
        raise CryptoError("message too short")
    sender_pub_sign = data[:SIGN_PUB_LEN]
    signed = data[SIGN_PUB_LEN:]
    plaintext = VerifyKey(sender_pub_sign).verify(signed)  # verifies signature
    return sender_pub_sign, plaintext


def encode_frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


async def read_frame(reader) -> bytes | None:
    """Read one length-prefixed frame from an asyncio StreamReader. None on EOF."""
    try:
        header = await reader.readexactly(4)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    (length,) = struct.unpack(">I", header)
    if length <= 0 or length > 16_000_000:
        return None
    try:
        return await reader.readexactly(length)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
