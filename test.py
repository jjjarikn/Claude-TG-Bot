import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from anthropic import Anthropic
from anthropic._exceptions import AuthenticationError

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / "claude.env"
loaded = load_dotenv(ENV_PATH)

CLAUDE_KEY = os.getenv("CLAUDE_KEY")
PROXY_URL = os.getenv("PROXY_URL")

print("BASE_DIR:", BASE_DIR)
print("ENV_PATH:", ENV_PATH)
print("ENV exists:", ENV_PATH.exists())
print("load_dotenv result:", loaded)
print("CLAUDE_KEY present:", bool(CLAUDE_KEY))
print("CLAUDE_KEY repr:", repr(CLAUDE_KEY))
print("CLAUDE_KEY len:", len(CLAUDE_KEY) if CLAUDE_KEY else None)
print("CLAUDE_KEY startswith sk-ant-:", bool(CLAUDE_KEY and CLAUDE_KEY.startswith("sk-ant-")))
print("PROXY_URL:", PROXY_URL if PROXY_URL else "not set")

timeout = httpx.Timeout(connect=10.0, read=10.0, write=10.0, pool=10.0)
http_client = httpx.Client(proxy=PROXY_URL, timeout=timeout) if PROXY_URL else httpx.Client(timeout=timeout)

client = Anthropic(
    api_key=CLAUDE_KEY,
    http_client=http_client,
    timeout=10.0
)

try:
    print("Sending test request...")
    resp = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=32,
        messages=[{"role": "user", "content": "Ответь одним словом: привет"}]
    )
    print("SUCCESS:", resp.content[0].text)
except AuthenticationError as e:
    print("AUTH ERROR:", e)
    print("Conclusion: request reached Anthropic, but the key was rejected.")
except Exception as e:
    print("OTHER ERROR:", type(e).__name__, e)
    print("Conclusion: if this is timeout/proxy error, the proxy/network is the issue.")
finally:
    http_client.close()