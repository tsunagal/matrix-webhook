[日本語](README.ja.md) | [English](README.md)

# Matrix Webhook Application Service

This repository contains a Matrix Application Service that provides webhook functionality for sending messages to Matrix rooms. It allows external services to send messages to Matrix rooms via simple HTTP POST requests.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Initial Setup](#initial-setup)
3. [Environment Variables](#environment-variables)
4. [Configuration](#configuration)
5. [Running the Service](#running-the-service)
6. [End-to-end encryption](#end-to-end-encryption)
7. [Usage Examples](#usage-examples)

---

## Prerequisites

- Docker & Docker Compose (v3.8+)
- A running Matrix homeserver (Synapse)
- Python 3.8+ (if running without Docker)

---

## Initial Setup

1. Clone this repository
2. The Application Service configuration file `webhook_as.yaml` is located in the matrix-server directory. Find more details [here](https://github.com/bshock-matrix/matrix-server?tab=readme-ov-file#g-webhook-application-service).

---

## Environment Variables

Copy `.env.example` to `.env` and configure the following variables:

```bash
# Matrix Webhook Environment Variables
MATRIX_URL=http://host.docker.internal:8008  # URL of your Matrix homeserver, eg. https://matrix.org
SERVER_NAME=localhost                        # Your Matrix server name, it should match the server_name in your homeserver.yaml config
BOT_LOCALPART=webhook_bot                   # Bot username localpart
AS_TOKEN=<generated>                        # Application Service token randomly generated
CRYPTO_DB_DIR=/data                         # Where the bot keeps its encryption keys (mount a persistent volume here)
RECOVERY_KEY=                               # The bot's recovery key; needed again when the /data volume is lost
PICKLE_KEY=matrix-webhook                   # Optional: key used to encrypt the stored Olm account
```

These environment variables are used to configure the webhook service. The `.env` file should be kept secure and never committed to version control.

---

## Running the Service

### Using Docker Compose (Recommended)

Start the service in detached mode:

```bash
docker-compose up -d
```

The port is published on `127.0.0.1:9000` only (this machine). Check it with `curl http://localhost:9000/health`. Set `url: null` in the appservice registration: the service never receives pushes from Synapse, and a `url` such as `http://localhost:9000` makes Synapse retry against itself.

`/webhook` has no authentication, so do not publish it on `0.0.0.0`. To receive posts from other machines, put a reverse proxy (Caddy, nginx) in front of it and expose only the `/webhook` path (ideally a secret path), not `/health`. When the service runs inside the same Docker network as the proxy (for example in the Synapse compose), it needs no published port: the proxy reaches it at `http://webhook:9000`.

### Without Docker

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Run the service:
   ```bash
   python main.py
   ```

The service will listen on port 9000 by default.

---

## End-to-end encryption

Messages to encrypted rooms are sent encrypted. The bot has its own device and keeps its keys in a SQLite file under `CRYPTO_DB_DIR` (`/data` in Docker).

- **Homeserver**: the appservice registration needs `io.element.msc4190: true` (the bot creates its own device; Synapse 1.159 needs no other setting). Synapse then requires the bot's registration to use `inhibit_login`, which the service does by itself.
- **Persistent volume**: `/data` must survive restarts (`./data:/data` in `docker-compose.yml`). Run only one instance.
- **Recovery key**: on the first start the bot creates its cross-signing identity and writes the recovery key to `/data/recovery_key.txt`. Store it with the other secrets, put it in `RECOVERY_KEY`, and delete the file.
- **If `/data` is lost**: the bot creates a new device and deletes its old ones. With `RECOVERY_KEY` set it signs the new device with the same identity, so members keep trusting it.
- The bot only syncs memberships, encryption state and key traffic; it never syncs message contents. New members and new devices receive the key with the next post. Posts made before someone joined stay unreadable to them, as with any member's messages.

---

## Usage Examples

### Sending a Message

Send a message to a room using curl:

```bash
curl -X POST http://localhost:9000/webhook \
     -H 'Content-Type: application/json' \
     -d '{"text":"Webhook via AS works test!","channel":"#test:localhost"}'
```

Each post opens a new thread, the same way Tsunagal does: the thread root holds the title and the first reply holds the body.

Successful response:

```json
{
  "ok": true,
  "event_id": "$VLnAIICMyUu8m39dA6lftWHz11B-WJzsRUtNuYg5mjQ",
  "thread_root": "$NhJ6sR3_nRiKwxQE-MVmQr0NlvOtVE494UQE7j3Uddk"
}
```

`event_id` is the body (the reply), `thread_root` the root of the thread. With `"thread": false` it is `null`.

### Request fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `text` | string | — | The message. Required, except when `title` is given for a new thread. |
| `channel` / `room_id` | string | — | Room alias (`#room:server`) or room ID (`!id:server`). One of them is required. |
| `format` | string | plain | `"html"`: `text` is HTML. It is sanitised and applies to the body; the thread root is always plain text. |
| `title` | string | — | Thread title. Line breaks and repeated spaces are collapsed; cut to 60 characters like a derived title. |
| `thread` | boolean | `true` | `false` sends one flat message as before. |
| `thread_root` | string | — | Event ID of an existing thread root in the same room: the post becomes one reply in that thread. |
| `thread_ts` | string | — | Slack-style alias of `thread_root`, used only when it is an event ID (starts with `$`). Slack timestamps such as `"1503435956.000247"` or numbers are ignored and a new thread is created, as before. If both are given they must be equal. |

### Title and body

- With `title`: the root is `title` and the body is the whole `text`. If `text` is empty, the body is the same placeholder Tsunagal uses for an empty thread, `ここから会話を始めます。`.
- Without `title`: the root is the first non-empty line of `text` (for `format: "html"`, of its plain-text rendering, never raw HTML).
  - A title longer than 60 characters is cut to 59 and ends with `…`. Emoji are never split (skin tones, ZWJ sequences, flags and keycaps stay whole or are dropped together).
  - If `text` has more lines and the title was not cut, the body is the remaining lines. Otherwise (one line, or the title was cut) the body is the whole `text`. With `format: "html"` the body is always the whole `text`, so the markup stays intact.
- If the body cannot be sent after the root was sent, the root is redacted, so no thread without a body is left, and the service answers 500.

```bash
# Title and body
curl -X POST http://localhost:9000/webhook \
     -H 'Content-Type: application/json' \
     -d '{"title":"Deploy finished","text":"tsunagal-web v2026.11.0 is live","channel":"#test:localhost"}'

# Reply in the thread created above
curl -X POST http://localhost:9000/webhook \
     -H 'Content-Type: application/json' \
     -d '{"text":"Rolled back","thread_root":"$NhJ6sR3_nRiKwxQE-MVmQr0NlvOtVE494UQE7j3Uddk","channel":"#test:localhost"}'

# One flat message, no thread
curl -X POST http://localhost:9000/webhook \
     -H 'Content-Type: application/json' \
     -d '{"text":"ping","thread":false,"channel":"#test:localhost"}'
```

### Message Format Options
> Note: The webhook bot can join public rooms automatically. For private rooms, invite the bot user to the room first, it will automatically accept the invitation.

You can send messages with different formats:

```bash
# HTML-formatted message
curl -X POST http://localhost:9000/webhook \
     -H 'Content-Type: application/json' \
     -d '{
           "text": "<b>Bold text</b> and <i>italic text</i>",
           "channel": "#test:localhost",
           "format": "html"
         }'

# Plain text with mentions
curl -X POST http://localhost:9000/webhook \
     -H 'Content-Type: application/json' \
     -d '{
           "text": "Hey @user:example.com, check this out!",
           "channel": "#test:localhost"
         }'
```

---

## Error Handling

The service returns appropriate HTTP status codes and JSON responses:

- 200: Message sent successfully
- 400: Invalid request: the body is not a JSON object, a field has the wrong type, `text` is missing, or `thread_root` is not a thread root (a reply, a relation, a state event or a redacted event)
- 403: The bot may not join or post in the room (for example a private room it was not invited to)
- 404: Room or `thread_root` not found
- 500: Internal server error

Example error response:

```json
{
  "ok": false,
  "error": "Room not found"
}
```
