"""End-to-end conversation over the Meshenger protocol, on loopback.

A simulated phone places a real call (sealed-box signalling over TCP, real
WebRTC media via aiortc), speaks, and must hear the agent's reply. The speech
models are replaced by fakes; everything else is the production path:
signalling, DTLS-SRTP media, the energy segmenter, the agent turn and the
outbound audio track.

Run: python -m pytest -q
"""
import asyncio
import fractions
import json
import math
import os
import struct

import pytest
from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame

import mesh_voice
from mesh_crypto import (decrypt_message, encode_frame, encrypt_message,
                         public_key_from_seed, read_frame)

pytest.importorskip("agent_voice")
from agent_voice.pipeline import VoicePipeline  # noqa: E402


def tone(seconds, rate, hz=440, amp=12000):
    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *(int(amp * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)))


class SpeakingTrack(AudioStreamTrack):
    """The caller's microphone: silence, then `speech_s` of sound, then silence."""

    def __init__(self, speech_s=1.5, lead_s=1.0):
        super().__init__()
        self.rate, self.samples, self.t = 48000, 960, 0
        self.lead, self.end = lead_s, lead_s + speech_s
        self.speaking_enabled = asyncio.Event()

    async def recv(self):
        await asyncio.sleep(0.02)
        now = self.t / self.rate
        loud = self.speaking_enabled.is_set() and self.lead <= now < self.end
        pcm = tone(0.02, self.rate) if loud else b"\x00" * (self.samples * 2)
        if self.speaking_enabled.is_set():
            self.t += self.samples
        frame = AudioFrame(format="s16", layout="mono", samples=self.samples)
        frame.planes[0].update(pcm)
        frame.sample_rate = self.rate
        frame.pts = getattr(self, "_pts", 0)
        self._pts = frame.pts + self.samples
        frame.time_base = fractions.Fraction(1, self.rate)
        return frame


class FakeSTT:
    def __init__(self):
        self.heard = []

    def transcribe(self, pcm16k):
        self.heard.append(len(pcm16k))
        return "what time do you close"


class FakeTTS:
    def __init__(self):
        self.spoken = []

    async def synth(self, text):
        self.spoken.append(text)
        return tone(1.0, 48000, hz=880)


def rms(frame):
    data = bytes(frame.planes[0])[: frame.samples * 2 * len(frame.layout.channels)]
    vals = struct.unpack(f"<{len(data)//2}h", data)
    return math.sqrt(sum(v * v for v in vals) / max(1, len(vals)))


async def place_call(port, bot_pub, mic):
    phone_seed = os.urandom(32)
    pc = RTCPeerConnection()
    pc.addTrack(mic)
    pc.createDataChannel("data")
    heard = []

    @pc.on("track")
    def on_track(track):
        async def listen():
            try:
                while True:
                    heard.append(rms(await track.recv()))
            except Exception:
                pass
        asyncio.ensure_future(listen())

    await pc.setLocalDescription(await pc.createOffer())
    while pc.iceGatheringState != "complete":
        await asyncio.sleep(0.05)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(encode_frame(encrypt_message(
        json.dumps({"action": "call", "offer": pc.localDescription.sdp}).encode(),
        bot_pub, phone_seed)))
    await writer.drain()
    for expected in ("ringing", "connected"):
        sender, plaintext = decrypt_message(await read_frame(reader), phone_seed)
        assert sender == bot_pub
        msg = json.loads(plaintext)
        assert msg["action"] == expected
    await pc.setRemoteDescription(RTCSessionDescription(sdp=msg["answer"], type="answer"))
    return pc, writer, heard


async def wait_for(cond, timeout=30):
    for _ in range(int(timeout / 0.1)):
        if cond():
            return True
        await asyncio.sleep(0.1)
    return False


async def test_caller_speaks_and_hears_the_agent_reply():
    seed = os.urandom(32)
    stt, tts = FakeSTT(), FakeTTS()

    def factory(outbound):
        p = VoicePipeline("test-bot", outbound, agent_backend="echo")
        p.stt, p.tts = stt, tts
        return p

    ep = mesh_voice.VoiceEndpoint("test-bot", seed, "127.0.0.1", 0, ring_seconds=0.1,
                                  pipeline_factory=factory)
    server = await asyncio.start_server(ep.on_connect, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    mic = SpeakingTrack()
    pc, writer, heard = await place_call(port, public_key_from_seed(seed), mic)
    try:
        assert await wait_for(lambda: pc.connectionState == "connected"), "media never connected"
        # the agent greets once media is up, and the caller hears it
        assert await wait_for(lambda: len(tts.spoken) >= 1), "no greeting"
        assert await wait_for(lambda: any(v > 500 for v in heard)), "greeting not heard"
        await asyncio.sleep(1.5)                      # let the greeting finish
        mic.speaking_enabled.set()                    # caller talks
        assert await wait_for(lambda: stt.heard), "caller speech never reached speech-to-text"
        assert await wait_for(lambda: len(tts.spoken) >= 2), "agent never replied"
        assert tts.spoken[-1] == "You said: what time do you close"
        before = len(heard)
        assert await wait_for(lambda: any(v > 500 for v in heard[before:])), "reply not heard"
    finally:
        writer.close()
        await pc.close()
        for c in list(ep.calls):
            await c.close()
        server.close()


def test_offline_mode_names_every_cloud_dependency(tmp_path):
    voice = tmp_path / "v.onnx"
    voice.write_bytes(b"x")
    local = {"LLM_BASE_URL": "http://127.0.0.1:11434/v1", "AGENT_VOICE_PIPER_VOICE": str(voice)}
    assert mesh_voice.offline_problems("generic", local) == []
    assert mesh_voice.offline_problems("generic", dict(local, LLM_BASE_URL="http://192.168.1.20:8080/v1")) == []
    cloud = mesh_voice.offline_problems("generic", dict(local, LLM_BASE_URL="https://api.deepseek.com"))
    assert len(cloud) == 1 and "api.deepseek.com" in cloud[0]
    assert any("not a local model" in p for p in mesh_voice.offline_problems("deepseek", local))
    missing = mesh_voice.offline_problems("generic", dict(local, AGENT_VOICE_PIPER_VOICE="/nope.onnx"))
    assert any("Piper" in p for p in missing)
