# Godot Relay — README

> **A relay/signaling server for multiple Godot multiplayer games**
>
> Components:
>
> * **Signal** (FastAPI WebSocket): relays WebRTC signaling (offer/answer/ICE), manages rooms (per game), access control (public/private with password), capacities, listings.
> * **TURN/STUN** (coturn): NAT traversal & fallback relay when P2P fails.
> * **Godot clients**: create/join rooms, establish WebRTC DataChannels, run the game logic.

---

## 0) TL;DR Quick Start

```bash
# Folder layout
.
├─ docker-compose.yml
├─ .env                    # optional but recommended (SIGNAL_SALT, etc.)
├─ turn/
│  └─ turnserver.conf      # coturn configuration
└─ signal/
   └─ app.py               # signaling server

# Start
docker compose up -d

# NGINX proxies wss://signal.example.tld/ws → 127.0.0.1:18080/ws
# TURN listens directly on 3478 UDP/TCP (and optionally 5349/TLS)
```

---

## 1) Architecture & Responsibilities

**Signal (WebSocket):**

* *Rooms per game*: key `(game, room)` → `Room`.
* *Access control*: `public` (no password) or `private` (password required, HMAC-hashed server-side).
* *Capacity*: bounded by `DEFAULT_CAPACITY`..`MAX_CAPACITY`.
* *Host election*: the **creator** (first peer) becomes `host_id`; auto re-election on host leave.
* *Listing*: list joinable rooms **by game** via HTTP or WS.
* *Signaling relay*: forwards `offer`/`answer`/`ice` with `{from, to}`.

**TURN/STUN (coturn):**

* STUN discovers public endpoints for ICE.
* TURN relays traffic if direct P2P fails (NAT symmetric, enterprise networks, CGNAT).

**Godot clients:**

* UI & gameplay.
* Initiate WebRTC connections (create data channels, send/receive game packets).

---

## 2) Requirements

* Docker & Docker Compose
* NGINX (or equivalent) with valid TLS for **WSS**
* Public IP server (no home router port-forward in this setup)

---

## 3) Deploy

### 3.1 Environment file (`.env`) — recommended

```dotenv
# SECURITY — set a long, random secret (used to HMAC passwords)
SIGNAL_SALT=CHANGEME-ULTRA-LONG-RANDOM

# Room capacity defaults
DEFAULT_CAPACITY=8
MAX_CAPACITY=16

# Optional string length limits
MAX_LEN_GAME=64
MAX_LEN_ROOM=64
MAX_LEN_NAME=64
```

### 3.2 TURN configuration (`turn/turnserver.conf`)

```conf
listening-port=3478
# tls-listening-port=5349           # enable later if you want TURN over TLS
realm=example.tld
fingerprint
lt-cred-mech
user=webrtcuser:webrtcsupersecret   # demo creds — replace in prod
simple-log
log-file=stdout
min-port=49160
max-port=49200
# external-ip=PUBLIC_IP             # only if your server sits behind another NAT
```

**Firewall**

* Allow **UDP 3478**, **TCP 3478** (TURN/STUN)
* Allow **UDP 49160–49200** (TURN relay port range)
* Allow **TCP 443** (NGINX TLS)

### 3.3 Docker Compose (`docker-compose.yml`)

```yaml
version: "3.9"

services:
  turn:
    image: coturn/coturn:latest
    command: ["-c", "/etc/coturn/turnserver.conf"]
    volumes:
      - ./turn/turnserver.conf:/etc/coturn/turnserver.conf:ro
    ports:
      - "3478:3478/udp"   # STUN/TURN UDP
      - "3478:3478/tcp"   # TURN TCP (fallback)
    restart: unless-stopped

  signal:
    image: python:3.12-slim
    working_dir: /app
    volumes:
      - ./signal:/app:ro
    env_file:
      - ./.env
    command: >
      sh -c "pip install --no-cache-dir fastapi==0.115.0 uvicorn[standard]==0.30.6
      && uvicorn app:app --host 0.0.0.0 --port 8080"
    ports:
      - "127.0.0.1:18080:8080"   # local-only; expose via NGINX (WSS)
    restart: unless-stopped
```

### 3.4 NGINX (WSS proxy)

```nginx
server {
  listen 443 ssl http2;
  server_name signal.example.tld;

  ssl_certificate     /etc/letsencrypt/live/signal.example.tld/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/signal.example.tld/privkey.pem;

  location /ws {
    proxy_pass http://127.0.0.1:18080/ws;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600;
    proxy_send_timeout 3600;
  }

  location / { return 200 "OK\n"; add_header Content-Type text/plain; }
}
```

> TURN **UDP** is not proxied by NGINX — it’s exposed directly on `3478`.

---

## 4) API Reference

### 4.1 WebSocket endpoint

```
wss://signal.example.tld/ws
```

Messages are JSON. The **first** client message must be one of: `create`, `join`, `list`.

#### Common types

* `error` — `{ "type":"error", "detail":"..." }`
* `hello` — room welcome (on successful create/join)
* `peer_joined`, `peer_left`, `new_host` — room membership events
* `rooms` — listing response (WS `list` or HTTP `/rooms`)

#### `create` — create a room (creator becomes host)

```json
{
  "type": "create",
  "game": "Hexoria",
  "room": "fr-1",
  "name": "Alice",
  "private": true,
  "pwd": "secret123",   // required if private=true
  "capacity": 6          // optional; clamped to [1..MAX_CAPACITY]
}
```

**Responses**

* `hello`:

```json
{
  "type": "hello",
  "game": "Hexoria",
  "room": "fr-1",
  "your_id": 3,
  "host_id": 3,
  "private": true,
  "capacity": 6,
  "players": 1,
  "peers": []
}
```

* On error: `error` and connection closes.

#### `join` — join existing room

```json
{
  "type": "join",
  "game": "Hexoria",
  "room": "fr-1",
  "name": "Bob",
  "pwd": "secret123"    // omit if room is public
}
```

**Responses**

* `hello` (with existing peers listed), and broadcast to others:

```json
{"type":"peer_joined","peer_id":4,"name":"Bob","players":2}
```

* Errors: `room not found`, `room is full`, `invalid password`.

#### `list` — list rooms (WS)

As first message (one-shot):

```json
{"type":"list","game":"Hexoria","only_public":false,"not_full":true}
```

Or anytime during a session:

```json
{"type":"list","game":"Hexoria"}
```

**Response**

```json
{
  "type": "rooms",
  "game": "Hexoria",
  "rooms": [
    {
      "game": "Hexoria",
      "room": "fr-1",
      "private": true,
      "capacity": 6,
      "players": 2,
      "host_id": 3,
      "created_at": 1730000000,
      "full": false
    }
  ]
}
```

#### Signaling relay — `offer`, `answer`, `ice`

Client → Server (must include `to`):

```json
{"type":"offer","to":4,"sdp":"..."}
{"type":"answer","to":3,"sdp":"..."}
{"type":"ice","to":4,"candidate":"...","sdpMid":"0","sdpMLineIndex":0}
```

Server → Receiver (server adds `from`):

```json
{"type":"offer","from":3,"to":4,"sdp":"..."}
{"type":"answer","from":4,"to":3,"sdp":"..."}
{"type":"ice","from":3,"candidate":"...","sdpMid":"0","sdpMLineIndex":0}
```

#### `leave`

```json
{"type":"leave"}
```

Server closes the socket; others receive:

```json
{"type":"peer_left","peer_id":4,"players":1}
```

If the leaver was host, server broadcasts:

```json
{"type":"new_host","host_id":3}
```

### 4.2 HTTP endpoint — list rooms

```
GET https://signal.example.tld/rooms?game=Hexoria&only_public=false&not_full=true
```

**Query params**

* `game` (required)
* `only_public` (default `false`)
* `not_full` (default `true`)

**Response** — same shape as WS `rooms` message.

---

## 5) Client (Godot) Cheat Sheet

```gdscript
# ICE servers (match your coturn setup)
const ICE := {
  "iceServers": [
    {"urls": ["stun:turn.example.tld:3478"]},
    {"urls": ["turn:turn.example.tld:3478?transport=udp"], "username": "webrtcuser", "credential": "webrtcsupersecret"}
  ]
}

# 1) Connect WebSocket → send `create` (host) or `join` (client)
# 2) Host: for each newcomer, create RTCPeerConnection, DataChannel("game"), make offer.
# 3) Non-host: on `offer`, setRemoteDescription → createAnswer.
# 4) Exchange ICE candidates both directions.
# 5) On DataChannel open, start sending gameplay packets.
```

> For a full example GDScript, see your project’s `Matchmaker.gd` (host creates to each peer; clients await offer).

---

## 6) Testing with Postman (no CLI)

### A) WebSocket — Create public room

1. New **WebSocket Request** → `wss://signal.example.tld/ws` → **Connect**.
2. Send:

```json
{"type":"create","game":"Hexoria","room":"pub-1","name":"A","private":false,"capacity":6}
```

3. Expect `hello`.
4. Open a second WS tab → **Connect**; send:

```json
{"type":"join","game":"Hexoria","room":"pub-1","name":"B"}
```

5. First tab receives `peer_joined`.

### B) WebSocket — Create private room & wrong password

1. Create:

```json
{"type":"create","game":"Hexoria","room":"priv-1","name":"A","private":true,"pwd":"secret"}
```

2. Join with wrong pwd → expect `error` `invalid password`.

```json
{"type":"join","game":"Hexoria","room":"priv-1","name":"B","pwd":"WRONG"}
```

3. Join with correct pwd → `hello`.

### C) WebSocket — Capacity limit

1. Create with `capacity=2`.
2. Join peer 2 → `hello`.
3. Join peer 3 → expect `error` `room is full`.

### D) WebSocket — Room listing (WS)

Send (one-shot connection):

```json
{"type":"list","game":"Hexoria","only_public":false,"not_full":true}
```

Expect a `rooms` message with joinable rooms for **Hexoria**.

### E) HTTP — Room listing

**GET** `https://signal.example.tld/rooms?game=Hexoria&only_public=false&not_full=true`

---

## 7) Security & Hardening

* **Change credentials**: TURN user/pass, and `SIGNAL_SALT` in `.env`.
* Prefer **TURN over TLS** (enable `tls-listening-port=5349` and use `turns:` URL) for restrictive networks.
* Add **rate limiting** on NGINX `/ws` to mitigate abuse.
* Consider **JWT auth** for `create/join` (issue tokens from your API, validate before accepting).
* Implement room **TTL** cleanup if you want automatic expiry.

---

## 8) Operations

* Start/Stop: `docker compose up -d`, `docker compose down`
* Logs: `docker logs -f signal`, `docker logs -f turn`
* Update: edit files → `docker compose up -d` (will restart with new config)

---

## 9) Troubleshooting

* **WS connect fails**: check TLS, NGINX `/ws` upgrade headers, local bind `127.0.0.1:18080`.
* **No DataChannel**: verify ICE servers, TURN firewall (3478 UDP/TCP + 49160–49200 UDP), test from two different networks.
* **Room not listed**: ensure correct `game`, not `full` if you filter, and that the room still has members.
* **Host left**: server broadcasts `new_host`; promote client logic accordingly.

---

## 10) Glossary

* **ICE**: algorithm trying multiple candidates (local, STUN, TURN) to connect peers.
* **STUN**: tells you your public address for NAT traversal.
* **TURN**: relays traffic if direct P2P fails.
* **Signaling**: out-of-band negotiation (SDP/ICE) before media/data flow.

---

## 11) Configuration Summary (server side)

* `.env`: `SIGNAL_SALT`, capacities, length limits.
* `turnserver.conf`: realm, credentials, ports, (optional) TLS.
* NGINX: WSS proxy for `/ws`.
* Firewall: 3478 UDP/TCP, 49160–49200 UDP, 443 TCP.

---

## 12) License & Version

* Internal project. Version `0.1` of the signaling API (this README). Update numbers when you change message shapes or semantics.
