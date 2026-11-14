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
- `test_phone.py` — simulated Meshenger caller (aiortc + same crypto) for a deployed bot
- `test_conversation.py` — automated loopback conversation test
- `venv/` — Python env (qrcode, pillow, pynacl, aiortc)

## Conversation

Inbound audio runs through agent-voice's pipeline: speech-to-text, the
card-grounded agent, text-to-speech, and the reply on an outbound audio track.
Signalling, the Ed25519 identity and the WebRTC session stay Meshenger's own;
only the conversation pipeline is shared, so a business has one agent whether
it is called over Nostr or over the LAN.

```bash
pip install pynacl aiortc qrcode pillow
pip install -e ../agent-voice            # the shared pipeline
export LLM_BASE_URL=http://127.0.0.1:11434/v1 LLM_API_KEY=local LLM_MODEL=<model>
python mesh_voice.py --bot <id> --bind <ip> --card /etc/helix/companies/<slug>/card.json
```

`--offline` refuses to start unless everything is local: the model server is on
this machine or the LAN, a Piper voice file is present, and the speech-to-text
model is already on disk. It prints each thing that would need the internet.
Without `--offline` the bot may use cloud services and must not be described
as working without internet.

`--no-agent --greeting hello.mp3` keeps the old greeting-only behaviour.

## Verified

- `python -m pytest -q`: a simulated phone places a real call on loopback
  (sealed-box signalling over TCP, WebRTC media via aiortc), speaks, and hears
  the agent's reply. Speech models are faked in the test; signalling, media,
  utterance segmentation, the agent turn and the outbound track are the real
  code.
- QR payload validated against the app's parser rules (`Contact.fromJSON`,
  `isValidName`, hex handling).

## Not yet verified

- A call from the real Meshenger Android app.
- A call with real speech models, and a fully offline call on real hardware
  (which model sizes a given machine can run in real time is untested).

## Add a bot

```bash
./venv/bin/python gen_bot_qr.py --bot <id> --name "<Display Name>" --ip <tailnet-ip>
# hand the user qr/<id>.png to scan; run mesh_voice.py --bot <id> --bind <tailnet-ip>
```
