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

Successful response:

```json
{
  "ok": true,
  "event_id": "$VLnAIICMyUu8m39dA6lftWHz11B-WJzsRUtNuYg5mjQ"
}
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
- 400: Invalid request format
- 401: Authentication failed
- 404: Room not found
- 500: Internal server error

Example error response:

```json
{
  "ok": false,
  "error": "Room not found"
}
```
