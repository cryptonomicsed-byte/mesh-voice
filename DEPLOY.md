# Deploying the Meshenger voice endpoint on grokbot-box

Run these in the box's terminal (Genspark). Takes ~5 minutes.

## 1. Get the code

```bash
git clone https://github.com/cryptonomicsed-byte/mesh-voice.git
cd mesh-voice
```

## 2. Install dependencies

```bash
python3 -m pip install --user aiortc pynacl
```

## 3. Start the endpoint

Replace `<KEY>` with the bot's 64-char secret key (Justin has it —
it's the secret half of the keypair in the QR the phone scanned):

```bash
nohup python3 mesh_voice.py \
  --bot grok-bot \
  --bind 100.109.5.90 \
  --key-hex <KEY> \
  --greeting greeting.mp3 \
  > mesh-voice.log 2>&1 &
```

Check it's listening:

```bash
tail -3 mesh-voice.log
# expect: [grok-bot] listening on 100.109.5.90:10001
```

## 4. Test from the phone

1. Open the Meshenger app — the **Grok Bot** contact should flip to **online**
   (the app pings the endpoint; a pong proves the crypto works).
2. Tap the contact → **Call**. The bot auto-answers and plays the greeting.
3. Hang up from either side.

## What v1 does and doesn't do

- ✅ Shows online, answers calls, full Meshenger-compatible signaling
- ✅ Plays the greeting MP3 so you hear the bot's voice
- ❌ Not yet: real conversation (speech-to-text → agent → text-to-speech).
  The hook is `VoiceEndpoint.on_audio_track()` in `mesh_voice.py` — that's the
  next build.

## Keeping it running

The `nohup ... &` survives the terminal closing but not a box reboot.
After a reboot, re-run step 3. (A systemd unit is the next step once v1
is proven.)
