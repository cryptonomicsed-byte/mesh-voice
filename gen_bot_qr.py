#!/usr/bin/env python3
"""Generate a Meshenger-compatible QR code for a bot.

The QR encodes the same contact JSON the Meshenger app itself puts in its
QR codes (see QRShowActivity / Contact.toJSON in the meshenger-android source):
    {"name": "<bot>", "public_key": "<ed25519 hex>", "addresses": ["<ip>", ...]}

A user scans it with the Meshenger app -> the bot appears as a contact ->
tapping "call" opens a TCP connection to the bot's IP on port 10001 and
runs the Meshenger signaling handshake (see mesh_voice.py).

Each bot gets its own ed25519 keypair. The secret key stays in
~/.config/mesh-voice/<bot>.key (mode 600); only the public key goes in the QR.
The voice endpoint needs the secret key to decrypt incoming signaling.

Usage:
    gen_bot_qr.py --bot grok-bot --name "Grok Bot" --ip 100.109.5.90 [--ip ...]
"""

import argparse
import json
import os
import stat
import sys

import qrcode
from nacl.signing import SigningKey

CONFIG_DIR = os.path.expanduser("~/.config/mesh-voice")
QR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr")


def load_or_create_keypair(bot):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    path = os.path.join(CONFIG_DIR, f"{bot}.key")
    if os.path.isfile(path):
        with open(path, "rb") as f:
            seed = f.read()
        print(f"loaded existing keypair for '{bot}'", file=sys.stderr)
    else:
        seed = os.urandom(32)
        with open(path, "wb") as f:
            f.write(seed)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        print(f"generated new keypair for '{bot}' -> {path}", file=sys.stderr)
    sk = SigningKey(seed)
    return sk, sk.verify_key.encode().hex()


def main():
    ap = argparse.ArgumentParser(description="generate a Meshenger QR code for a bot")
    ap.add_argument("--bot", required=True, help="bot id, e.g. grok-bot")
    ap.add_argument("--name", required=True, help="display name shown in the app")
    ap.add_argument("--ip", required=True, action="append",
                    help="reachable IP address (repeatable); the Meshenger app connects to these on TCP 10001")
    args = ap.parse_args()

    _, pub_hex = load_or_create_keypair(args.bot)

    contact = {
        "name": args.name,
        "public_key": pub_hex,
        "addresses": args.ip,
    }
    payload = json.dumps(contact, separators=(",", ":"))

    os.makedirs(QR_DIR, exist_ok=True)
    out = os.path.join(QR_DIR, f"{args.bot}.png")
    img = qrcode.make(payload, box_size=12, border=4)
    img.save(out)
    print(f"QR written to {out}", file=sys.stderr)
    print(payload)


if __name__ == "__main__":
    main()
