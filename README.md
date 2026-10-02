# mesh-voice — Meshenger-compatible voice endpoint for bots

A user scans the bot's QR code with the [Meshenger](https://github.com/meshenger-app/meshenger-android)
app, taps the bot contact, hits call — and talks to the agent. No accounts, no
servers, no internet: the app calls the bot's IP directly, exactly like
calling another Meshenger user.

## How it works

**QR code** (`gen_bot_qr.py`): encodes the exact contact JSON the Meshenger app
itself puts in QR codes — `{"name","public_key","addresses"}` — where the
public key is the bot's ed25519 identity and the addresses are the IPs the app
should call (TCP port 10001, `MainService.SERVER_PORT`). Each bot gets its own
keypair; the secret stays in `~/.config/mesh-voice/<bot>.key` (0600).

**Signaling** (`mesh_voice.py`): speaks the app's protocol byte-for-byte —
4-byte big-endian framing, `crypto_sign`+`crypto_box_seal` message crypto
(mirrors `Crypto.kt`), `ping`→`pong`, `call`→`ringing`→`connected`+SDP answer,
`keep_alive`, `dismissed`. The bot auto-answers; no human taps accept.

**Media**: standard WebRTC via aiortc. The app's SDP offer is answered, DTLS-SRTP
comes up, the user's audio arrives on an `audio` track, hangup arrives on the
`data` channel.

## Files

- `gen_bot_qr.py` — per-bot keypair + QR (`qr/<bot>.png`)
- `mesh_crypto.py` — Meshenger signaling crypto + framing (mirrors `Crypto.kt`, `PacketReader`/`PacketWriter`)
- `mesh_voice.py` — the endpoint: `--bot grok-bot --bind <tailnet-ip> [--port 10001]`
- `test_phone.py` — simulated Meshenger caller (aiortc + same crypto) for testing
- `venv/` — Python env (qrcode, pillow, pynacl, aiortc)

## Verified

- Full signaling handshake against the simulated caller: TCP connect →
  sealed `{"action":"call","offer"}` → `ringing` → `connected`+answer, with
  ed25519 mutual authentication (caller key checked on both ends). PASS.
- QR payload validated against the app's parser rules (`Contact.fromJSON`,
  `isValidName`, hex handling).

## Honest gaps

- **WebRTC media couldn't be tested in this sandbox** — it blocks UDP, and
  aiortc's ICE is UDP-only here. The offer/answer exchange itself works; on a
  normal host (e.g. grokbot-box) ICE will complete over the tailnet. Verify on
  deploy with a real Meshenger call.
- **Voice pipeline not yet built**: inbound audio → STT → agent → TTS →
  outbound audio track. The hook is `VoiceEndpoint.on_audio_track()`. TTS
  exists (`/opt/hatch/bin/tts`); STT needs a pick (faster-whisper); the agent
  brain needs a choice (DeepSeek is already wired via vault for HelixMain).
- **Deploy blocked**: needs the Tailscale tunnel approval for grokbot-box
  (still not surfacing). When it opens: copy `mesh_voice.py`,
  `mesh_crypto.py`, the key, and run with `--bind 100.109.5.90`.

## Add a bot

```bash
./venv/bin/python gen_bot_qr.py --bot <id> --name "<Display Name>" --ip <tailnet-ip>
# hand the user qr/<id>.png to scan; run mesh_voice.py --bot <id> --bind <tailnet-ip>
```
