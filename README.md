Step 1 — Clone & enter the directory
git clone <your_repo_url>
cd Assignment\ 4

Step 2 — Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On macOS/Linux
# .venv\Scripts\activate   # On Windows

Step 3 — Install dependencies
pip install aioquic uvloop


uvloop is optional but improves performance.

Step 4 — Generate self-signed TLS certificates

QUIC requires TLS 1.3 encryption.
Run the following command in the project directory:

openssl req -newkey rsa:2048 -nodes -keyout server.key \
    -x509 -days 365 -out server.crt \
    -subj "/CN=localhost"


This creates:

server.crt — the public certificate

server.key — the private key

Keep them in the same directory as server.py.

🚀 3. Running the Application
Terminal 1 — Start the QUIC Server
python3 server.py


Expected output:

[server] QUIC listening on 0.0.0.0:4433 (ALPN=game/1)


The server will remain running, waiting for QUIC clients to connect.

Terminal 2 — Start the Client
python3 client.py
