#!/usr/bin/env python3
"""mesh-voice: a Meshenger-compatible voice endpoint for bots.

A user scans the bot's QR code with the Meshenger app, taps the bot contact,
and hits call. The app opens TCP <bot-ip>:10001 and runs Meshenger's
signaling handshake (see RTCPeerConnection.kt / RTCCall.kt in
meshenger-android). This server speaks that protocol:

  phone -> bot : {"action":"ping"}                        -> {"action":"pong"}
  phone -> bot : {"action":"call","offer":"<SDP>"}        -> {"action":"ringing"}
                                                          -> {"action":"connected","answer":"<SDP>"}
  both ways    : {"action":"keep_alive"}  (every ~2.5s, 5s timeout)
  data channel : {"state_change":"hangup"}

Every signaling message is framed (4-byte big-endian length) and encrypted
exactly like the app: crypto_sign(own key) prepended with own public key,
then crypto_box_seal() to the peer's ed25519->curve25519 key.

Media is standard WebRTC (aiortc). The bot auto-answers — no human taps
"accept". Phase 2 (not yet wired): STT on the inbound audio track ->
agent -> TTS on the outbound track. See on_audio_track().

Usage:
    mesh_voice.py --bot grok-bot --bind 100.109.5.90 [--port 10001]
Key: ~/.config/mesh-voice/<bot>.key (created by gen_bot_qr.py)
"""

import argparse
import asyncio
import json
import os
import sys

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer

from mesh_crypto import (
    decrypt_message,
    encode_frame,
    encrypt_message,
    public_key_from_seed,
    read_frame,
)

CONFIG_DIR = os.path.expanduser("~/.config/mesh-voice")
SERVER_PORT = 10001


class VoiceEndpoint:
    def __init__(self, bot, seed, bind, port, ring_seconds=1.5, greeting=None):
        self.bot = bot
        self.seed = seed
        self.own_pub = public_key_from_seed(seed)
        self.bind = bind
        self.port = port
        self.ring_seconds = ring_seconds
        self.greeting = greeting
        self.calls = []
        self._players = []  # keep MediaPlayers alive for the call duration

    async def run(self):
        server = await asyncio.start_server(self.on_connect, self.bind, self.port)
        print(f"[{self.bot}] listening on {self.bind}:{self.port}", flush=True)
        async with server:
            await server.serve_forever()

    async def send(self, writer, recipient_pub, obj):
        payload = encrypt_message(json.dumps(obj).encode(), recipient_pub, self.seed)
        writer.write(encode_frame(payload))
        await writer.drain()

    async def on_connect(self, reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            raw = await read_frame(reader)
            if raw is None:
                return
            sender_pub, plaintext = decrypt_message(raw, self.seed)
            try:
                msg = json.loads(plaintext.decode())
            except (ValueError, UnicodeDecodeError):
                return
            action = msg.get("action")
            print(f"[{self.bot}] {peer} action={action}", flush=True)
            if action == "ping":
                await self.send(writer, sender_pub, {"action": "pong"})
            elif action == "call":
                await self.handle_call(reader, writer, sender_pub, msg)
            # status_change / unknown: ignore, close
        except Exception as e:
            print(f"[{self.bot}] connection error: {e}", flush=True)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def handle_call(self, reader, writer, sender_pub, msg):
        offer_sdp = msg.get("offer", "")
        if not offer_sdp:
            await self.send(writer, sender_pub, {"action": "dismissed"})
            return

        pc = RTCPeerConnection()
        self.calls.append(pc)

        # greeting: played to the caller when the call connects (v1 voice)
        if self.greeting and os.path.isfile(self.greeting):
            try:
                player = MediaPlayer(self.greeting)
                if player.audio:
                    pc.addTrack(player.audio)
                    player._voice_pc = pc  # tag for cleanup
                    self._players.append(player)
                    print(f"[{self.bot}] greeting track added", flush=True)
            except Exception as e:
                print(f"[{self.bot}] greeting failed: {e}", flush=True)

        @pc.on("track")
        def on_track(track):
            print(f"[{self.bot}] incoming {track.kind} track", flush=True)
            if track.kind == "audio":
                asyncio.ensure_future(self.on_audio_track(track))

        @pc.on("datachannel")
        def on_datachannel(channel):
            @channel.on("message")
            def on_message(message):
                try:
                    o = json.loads(message)
                except ValueError:
                    return
                print(f"[{self.bot}] datachannel: {o}", flush=True)
                if o.get("state_change") == "hangup":
                    asyncio.ensure_future(pc.close())

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            print(f"[{self.bot}] webrtc {pc.connectionState}", flush=True)
            if pc.connectionState in ("closed", "failed"):
                if pc in self.calls:
                    self.calls.remove(pc)
                self._players = [p for p in self._players
                                 if getattr(p, "_voice_pc", None) is not pc]

        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        # wait for ICE gathering so the answer carries real candidates
        while pc.iceGatheringState != "complete":
            await asyncio.sleep(0.1)

        await self.send(writer, sender_pub, {"action": "ringing"})
        await asyncio.sleep(self.ring_seconds)
        await self.send(
            writer, sender_pub,
            {"action": "connected", "answer": pc.localDescription.sdp},
        )
        print(f"[{self.bot}] call connected", flush=True)

        # The app closes the signaling socket once WebRTC is up; until then,
        # swallow keep_alives / dismissed. The RTCPeerConnection lives on.
        try:
            while True:
                raw = await read_frame(reader)
                if raw is None:
                    break
                try:
                    _, plaintext = decrypt_message(raw, self.seed)
                    m = json.loads(plaintext.decode())
                except Exception:
                    break
                if m.get("action") == "dismissed":
                    await pc.close()
                    break
        except (ConnectionError, asyncio.IncompleteReadError):
            pass

    async def on_audio_track(self, track):
        """Phase 2 hook: STT -> agent -> TTS. For now, consume and count."""
        frames = 0
        try:
            while True:
                frame = await track.recv()
                frames += 1
                if frames == 1:
                    print(f"[{self.bot}] first audio frame: {frame.format.name} "
                          f"{frame.sample_rate}Hz", flush=True)
        except Exception as e:
            print(f"[{self.bot}] audio track ended after {frames} frames ({e})", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Meshenger-compatible voice endpoint for a bot")
    ap.add_argument("--bot", required=True)
    ap.add_argument("--bind", required=True, help="IP to listen on (bot's tailnet IP)")
    ap.add_argument("--port", type=int, default=SERVER_PORT)
    ap.add_argument("--ring-seconds", type=float, default=1.5)
    ap.add_argument("--greeting", default=None,
                    help="MP3 played to the caller on connect (v1 voice)")
    ap.add_argument("--key-hex", default=None,
                    help="ed25519 seed as hex (overrides the key file; handy for deploy)")
    args = ap.parse_args()

    if args.key_hex:
        try:
            seed = bytes.fromhex(args.key_hex)
        except ValueError:
            sys.exit("bad --key-hex (expected 64 hex chars)")
        if len(seed) != 32:
            sys.exit("bad --key-hex (expected 32-byte seed)")
    else:
        key_path = os.path.join(CONFIG_DIR, f"{args.bot}.key")
        if not os.path.isfile(key_path):
            sys.exit(f"no key for bot '{args.bot}' at {key_path} "
                     f"(run gen_bot_qr.py first, or pass --key-hex)")
        with open(key_path, "rb") as f:
            seed = f.read()
        if len(seed) != 32:
            sys.exit("bad key file (expected 32-byte seed)")

    ep = VoiceEndpoint(args.bot, seed, args.bind, args.port,
                       args.ring_seconds, args.greeting)
    try:
        asyncio.run(ep.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
