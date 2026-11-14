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
"accept". Inbound audio runs through agent-voice's pipeline (speech-to-text ->
card-grounded agent -> text-to-speech) and the reply goes back on an outbound
audio track. Signalling, keys and the WebRTC session stay Meshenger's; only
the conversation pipeline is shared.

Usage:
    mesh_voice.py --bot grok-bot --bind 100.109.5.90 --card card.json
    mesh_voice.py ... --offline      # refuse to start unless everything is local
    mesh_voice.py ... --no-agent --greeting hello.mp3   # greeting only
Key: ~/.config/mesh-voice/<bot>.key (created by gen_bot_qr.py)
"""

import argparse
import asyncio
import ipaddress
import json
import os
import sys
from urllib.parse import urlparse

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
    def __init__(self, bot, seed, bind, port, ring_seconds=1.5, greeting=None,
                 pipeline_factory=None):
        # pipeline_factory(outbound_track) -> a VoicePipeline for one call, or
        # None for a greeting-only endpoint.
        self.pipeline_factory = pipeline_factory
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

        pipeline = None
        if self.pipeline_factory is not None:
            from agent_voice.pipeline import QueueAudioTrack, pump_audio
            outbound = QueueAudioTrack()
            pc.addTrack(outbound)
            pipeline = self.pipeline_factory(outbound)
        elif self.greeting and os.path.isfile(self.greeting):
            # greeting-only endpoint: play the MP3 when the call connects
            try:
                player = MediaPlayer(self.greeting)
                if player.audio:
                    pc.addTrack(player.audio)
                    player._voice_pc = pc  # tag for cleanup
                    self._players.append(player)
                    print(f"[{self.bot}] greeting track added", flush=True)
            except Exception as e:
                print(f"[{self.bot}] greeting failed: {e}", flush=True)

        greeted = False

        @pc.on("track")
        def on_track(track):
            print(f"[{self.bot}] incoming {track.kind} track", flush=True)
            if track.kind != "audio":
                return
            if pipeline is not None:
                asyncio.ensure_future(pump_audio(track, pipeline))
            else:
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
            nonlocal greeted
            print(f"[{self.bot}] webrtc {pc.connectionState}", flush=True)
            # Greet only once media is actually up; audio queued before the
            # transport connects is never heard.
            if pc.connectionState == "connected" and pipeline is not None and not greeted:
                greeted = True
                asyncio.ensure_future(pipeline.greet())
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
        """Greeting-only mode: drain the caller's audio (nothing listens to it)."""
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


def _is_local_host(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def offline_problems(backend: str, env=os.environ) -> list[str]:
    """Why this configuration would need the internet. Empty list = fully local.

    A LAN call only works without internet if speech-to-text, the model and
    text-to-speech all run locally; this is checked before the bot starts so
    an "offline" bot never silently depends on a cloud service.
    """
    problems = []
    if backend != "generic":
        problems.append(f"agent backend '{backend}' is not a local model; use "
                        "--agent-backend generic with LLM_BASE_URL on this machine or LAN")
    else:
        host = urlparse(env.get("LLM_BASE_URL", "")).hostname or ""
        if not _is_local_host(host):
            problems.append(f"LLM_BASE_URL host '{host}' is not on this machine or LAN")
    voice = env.get("AGENT_VOICE_PIPER_VOICE", "/opt/agent-voice/voices/en_US-lessac-medium.onnx")
    if not os.path.isfile(voice):
        problems.append(f"no local Piper voice at {voice} (set AGENT_VOICE_PIPER_VOICE)")
    return problems


def build_pipeline_factory(bot, card_path, backend, offline):
    """Return a per-call VoicePipeline factory, or exit with what is missing."""
    try:
        from agent_voice.__main__ import _load_card
        from agent_voice.pipeline import Transcriber, VoicePipeline
    except ImportError as e:
        sys.exit(f"agent-voice is required for conversation ({e}). Install it, or "
                 "run with --no-agent for a greeting-only endpoint.")
    card = _load_card(card_path) if card_path else None
    if offline:
        problems = offline_problems(backend)
        if problems:
            sys.exit("--offline refused; this setup needs the internet:\n  - "
                     + "\n  - ".join(problems))
        os.environ["AGENT_VOICE_TTS"] = "piper"      # no cloud TTS fallback
        try:
            Transcriber()._load()                    # the STT model must already be on disk
        except Exception as e:
            sys.exit(f"--offline refused; speech-to-text model is not available locally: {e}")

    def factory(outbound):
        return VoicePipeline(bot, outbound, agent_backend=backend, card=card)

    try:                                             # fail at boot, not on the first call
        from agent_voice.pipeline import QueueAudioTrack
        factory(QueueAudioTrack())
    except Exception as e:
        sys.exit(f"voice pipeline cannot start: {e}")
    return factory


def main():
    ap = argparse.ArgumentParser(description="Meshenger-compatible voice endpoint for a bot")
    ap.add_argument("--bot", required=True)
    ap.add_argument("--bind", required=True, help="IP to listen on (bot's tailnet IP)")
    ap.add_argument("--port", type=int, default=SERVER_PORT)
    ap.add_argument("--ring-seconds", type=float, default=1.5)
    ap.add_argument("--card", default=None,
                    help="business card.json to ground the agent on")
    ap.add_argument("--agent-backend", default=os.environ.get("MESH_VOICE_AGENT_BACKEND", "generic"),
                    choices=["generic", "deepseek"],
                    help="generic = any OpenAI-compatible server (LLM_BASE_URL, LLM_API_KEY, LLM_MODEL)")
    ap.add_argument("--offline", action="store_true",
                    help="refuse to start unless STT, the model and TTS are all local")
    ap.add_argument("--no-agent", action="store_true",
                    help="greeting-only endpoint: no conversation")
    ap.add_argument("--greeting", default=None,
                    help="MP3 played on connect (only with --no-agent)")
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

    factory = None
    if not args.no_agent:
        factory = build_pipeline_factory(args.bot, args.card, args.agent_backend, args.offline)
    print(f"[{args.bot}] mode: "
          + ("greeting only" if factory is None else
             f"conversation ({args.agent_backend}{', offline-checked' if args.offline else ''})"),
          flush=True)
    ep = VoiceEndpoint(args.bot, seed, args.bind, args.port,
                       args.ring_seconds, args.greeting, pipeline_factory=factory)
    try:
        asyncio.run(ep.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
