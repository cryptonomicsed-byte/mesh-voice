#!/usr/bin/env python3
"""Simulated Meshenger caller — exercises mesh_voice.py the way the real
Android app does on an outgoing call (see RTCPeerConnection.kt):

  1. TCP connect to bot:10001
  2. send {"action":"call","offer":"<aiortc SDP offer>"} (sealed-box encrypted)
  3. expect {"action":"ringing"}, then {"action":"connected","answer":"<SDP>"}
  4. complete the WebRTC handshake, wait for ICE/DTLS to connect
  5. hold briefly, then close

Usage:
    test_phone.py --bot-pubkey <hex> [--host 127.0.0.1] [--port 10001]
"""

import argparse
import asyncio
import json
import os
import struct
import sys

from aiortc import RTCPeerConnection, RTCSessionDescription

from mesh_crypto import decrypt_message, encode_frame, encrypt_message, read_frame


async def main():
    ap = argparse.ArgumentParser(description="simulated Meshenger caller")
    ap.add_argument("--bot-pubkey", required=True, help="bot's ed25519 public key (hex)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=10001)
    args = ap.parse_args()

    bot_pub = bytes.fromhex(args.bot_pubkey)
    phone_seed = os.urandom(32)

    # --- WebRTC offer (mirrors RTCCall.initOutgoing) ---
    pc = RTCPeerConnection()
    pc.addTransceiver("audio", direction="sendrecv")
    channel = pc.createDataChannel("data")
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    # like the app (onIceGatheringChange == COMPLETE), don't send the
    # offer until candidates are gathered
    while pc.iceGatheringState != "complete":
        await asyncio.sleep(0.1)
    print("[phone] offer created (ice gathered)")

    # --- signaling (mirrors createOutgoingCallInternal) ---
    reader, writer = await asyncio.open_connection(args.host, args.port)
    print(f"[phone] tcp connected to {args.host}:{args.port}")

    def sealed(obj):
        return encode_frame(encrypt_message(json.dumps(obj).encode(), bot_pub, phone_seed))

    writer.write(sealed({"action": "call", "offer": pc.localDescription.sdp}))
    await writer.drain()

    async def expect(*actions):
        raw = await read_frame(reader)
        assert raw is not None, "signaling socket closed unexpectedly"
        sender_pub, plaintext = decrypt_message(raw, phone_seed)
        assert sender_pub == bot_pub, "sender key mismatch (authentication failed)"
        msg = json.loads(plaintext.decode())
        assert msg.get("action") in actions, f"unexpected action: {msg}"
        return msg

    msg = await expect("ringing", "dismissed")
    assert msg["action"] == "ringing", "call was dismissed"
    print("[phone] ringing")
    msg = await expect("connected", "dismissed")
    assert msg["action"] == "connected", "call was dismissed"
    answer_sdp = msg.get("answer", "")
    assert answer_sdp, "connected without an SDP answer"
    print("[phone] connected, got answer")

    # --- complete WebRTC ---
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

    connected = asyncio.Event()

    @pc.on("connectionstatechange")
    async def on_state():
        print(f"[phone] webrtc {pc.connectionState}")
        if pc.connectionState == "connected":
            connected.set()

    try:
        await asyncio.wait_for(connected.wait(), timeout=15)
    except asyncio.TimeoutError:
        sys.exit("[phone] FAIL: WebRTC never connected")

    print("[phone] PASS: full call established (signaling + WebRTC media path up)")

    # exchange a datachannel ping to prove the channel works
    await asyncio.sleep(1)
    if channel.readyState == "open":
        channel.send(json.dumps({"state_change": "test"}))
        print("[phone] datachannel open, sent test message")

    await asyncio.sleep(2)
    writer.close()
    await pc.close()
    print("[phone] done")


if __name__ == "__main__":
    asyncio.run(main())
