# The curator on a Mac Studio

The «local» destination of the Curation panel is a **Mac Studio M2 Max (32 GB)**: the production environment's server (Xeon X5550)
has no AVX and no usable GPU (`CLAUDE.md`). Here `llama-server` from llama.cpp runs with **Metal**, as a **launchd** service,
speaking the OpenAI API (`/v1/chat/completions`, which `server/ariadne/curation/curator.py` expects). The Ariadne worker (the
Docker host where it runs) calls it over the LAN.

```
Docker host (worker) ──HTTP, Bearer <key>──▶  Mac:8085  llama-server (Metal, Gemma 3 12B Q4_K_M)
```

## The model (and why)

| | |
|---|---|
| Model | **Gemma 3 12B Instruct**, quantization **Q4_K_M** (GGUF) |
| File | `gemma-3-12b-it-Q4_K_M.gguf`, 7,300,574,976 bytes |
| Source | `huggingface.co/ggml-org/gemma-3-12b-it-GGUF`, revision `ec0cbabd8dbff316f659876a50202295c3c4a314` (no login) |
| sha256 | `7bb69bff3f48a7b642355d64a90e481182a7794707b3133890646b1efa778ff5` |
| Location | `~/Library/Application Support/ariadne-curator/models/gemma-3-12b-it-Q4_K_M.gguf` |

Everything is pinned in [`model.lock`](model.lock): `install.sh`/`update.sh` only accept a file that matches (they download
to `.part` with resume and only promote it after the sha256 check).

**Why this one.** The curator rewrites articles of up to 40,000 characters (~12 thousand tokens) into closed JSON, in Portuguese
and English, without losing numbers, names or doses (the fidelity check discards whatever is lost). It needs:

- **Reliable instruction and JSON following**, and to be **multilingual** (Gemma 3 covers European Portuguese well): a dense 12B
  does this clearly better than a 7–8B, and it is the candidate that `curator.py` itself already named.
- **Fitting comfortably in 32 GB of unified memory**, which the Mac shares with everything else (ComfyUI, the owner's Ollama…):
  the file is 7.3 GB and the KV cache for a 32k context costs a few GB (Gemma 3 uses local attention in 5 of every 6 layers),
  so the service stays below ~12 GB and memory is left over. A Q8_0 (12.5 GB) or a 27B model is not worth it: fidelity gains
  little and throughput (400 GB/s memory bandwidth) drops to almost half.
- **Licence:** Gemma Terms of Use (use for this purpose is permitted); the `ggml-org` repository has no access gate.

To change model, change `model.lock` (file, revision, size, sha256, alias) in a commit and run `./update.sh`.
`MODEL_ALIAS` (`gemma-3-12b-it`) is the value of `CURATOR_MODEL` in Ariadne.

## Install

On the Mac, with the repository:

```bash
cd deploy/mac-curator
./install.sh
```

In order, it: checks for `llama-server` (`brew install llama.cpp` if missing); downloads and **verifies** the model; creates the
API key (`openssl rand -hex 24`) in `~/Library/Application Support/ariadne-curator/api-key` (mode 600, **never printed**);
copies `serve.sh` and `model.lock` there; writes the plist to `~/Library/LaunchAgents/org.ariadne.curator.plist`; loads it and
waits for `/health`; runs `check.sh`. It is idempotent.

The service runs `llama-server` with: `--host $BIND --port 8085` (without `BIND`, `llama-server` listens on localhost only; for
the worker to reach it over the LAN, set `BIND` in `config.env`), **`--api-key-file`** (without a key it returns 401),
`--ctx-size 32768`, `--parallel 1` (one article at a time: the worker is sequential per job), `--n-gpu-layers 99 --flash-attn on`
(everything on Metal), `--no-mmproj` (no vision: saves memory), `--no-webui`, `--jinja`. Tuning settings without secrets go in
`~/Library/Application Support/ariadne-curator/config.env` (`PORT`, `BIND`, `CTX`, `THREADS`).
`KeepAlive` restarts it if it stops (with a 30 s interval); `RunAtLoad` starts it with the session.

**Starting with the Mac.** A LaunchAgent starts when the user logs in. With FileVault on, the Mac asks for the password at boot
anyway, so this is the normal case. To start before any session exists (for example after a power failure, without FileVault):
`sudo ./install.sh --daemon` (LaunchDaemon, runs as your user).

### On the Ariadne side (dashboard → Integrations)

| Field | Value |
|---|---|
| Curator → server URL | `http://<mac-ip>:8085` |
| Curator → Model | `gemma-3-12b-it` |
| Curator → API key | `./copy-key.sh` (copies the key **without showing it**; paste it and then run `pbcopy < /dev/null`) |
| Privacy and network → Allowed hosts | the **entire** list (`PRIVATE_ALLOWED_HOSTS`) with the Mac's IP appended |

And **Test connection** on the Curator card. Private-class text only goes to a host on this list (`STRUCTURE.md`, section 7).

### Network

- **Fixed IP:** a DHCP reservation for the Mac on your router/firewall (or a static IP), so the address does not change.
- **Network firewall:** the network of the worker's host must be able to reach `<mac-ip>:8085/tcp`. This is not automated.
- **macOS firewall:** currently off (`socketfilterfw --getglobalstate`), so nothing prompts. If you turn it on, allow incoming
  connections for `llama-server` (System Settings → Network → Firewall → Options), or the worker gets no answer.
- The port only answers with the key; even so, do not expose it outside the LAN.

## When the Mac is off

The worker checks `/health` (cached: 15 s if it is down, 60 s if it is up) and **only claims `curate_page` while it responds**.
Jobs stay in the queue exactly as they were (without using up attempts), and the Curation panel shows «Mac offline»; when the Mac
comes back, the queue starts by itself. If the Mac drops in the middle of a request, that job is postponed for 5 minutes and is
not counted as a failure. The rule «external GPU first, then the Mac» still applies.

## Operate

| | |
|---|---|
| `./check.sh` | `/health`, 401 without a key, 200 with a key and a short real request |
| `./update.sh [--brew]` | applies `model.lock` (downloads and verifies if it changed), reinstalls `serve.sh`, restarts; `--brew` updates llama.cpp first |
| `./copy-key.sh` | copies the key to the clipboard without showing it |
| `./uninstall.sh` | removes the service (keeps the model and key; `KEEP=0` deletes everything) |
| Logs | `~/Library/Logs/ariadne-curator/llama-server.log` |
| Status | `launchctl print gui/$(id -u)/org.ariadne.curator` |
| Rotate the key | delete `~/Library/Application Support/ariadne-curator/api-key`, run `./install.sh`, then `./copy-key.sh` and paste into the dashboard |

## Verified installation (26/10/2026, Mac Studio M2 Max, macOS 26, llama.cpp 10210)

- 7.3 GB download in ~5 min; **sha256 matches `model.lock`**; key created (mode 600), never printed.
- Port **8080 was already in use** (Docker): the service uses **8085** (`PORT` in `config.env`).
- Memory of `llama-server` with the model loaded and a 32k context: **~9 GB** RSS; the rest of the Mac stays free.
- Speed: an article of ~860 characters curated in ~13 s (≈ 20 tokens/s generation, prompt reading included).
  An article of 40,000 characters (~12 thousand output tokens) takes on the order of 8–10 minutes: suitable for a queue, not
  for interaction.
- `check.sh`: `/health` ok; **no key 401, wrong key 401, right key 200** (note: `/health` and `/v1/models` are public in
  llama-server; what requires the key is `/v1/chat/completions`, and that is what `check.sh` and the dashboard's «Test
  connection» test).
- `kill -9` on the server: launchd brings it back by itself (`KeepAlive`).
- A Docker container on the Mac reaches `http://<mac-ip>:8085/health` through the LAN IP (the same path the worker will use).
- **End to end:** `tests/test_curator_mac_live.py` (opt-in, `TEST_CURATOR_URL`) configures the curator **through the vault** (URL,
  model and key), queues a health-topic article, and the worker processes the `curate_page` on the Mac; the decision (and the fidelity
  check) was recorded with `curator_model = gemma-3-12b-it`. On a tiny test article Gemma translated «vezes» as «times»; the
  fidelity check caught it and the item was left for human review (this is what it is for). On a realistic article in
  Portuguese it kept the language and the numbers.
