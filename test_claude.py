"""Quick test — Claude API hello world."""
import os
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("ANTHROPIC_API_KEY", "")
print(f"API key present: {bool(api_key)}")
print(f"Key starts with: {api_key[:20]}...")

import anthropic

client = anthropic.Anthropic(api_key=api_key)

msg = client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=50,
    messages=[{"role": "user", "content": "Say exactly: Hello World from Claude!"}],
)

print(f"\nClaude says: {msg.content[0].text}")
print(f"Model used: {msg.model}")
print(f"Input tokens: {msg.usage.input_tokens}")
print(f"Output tokens: {msg.usage.output_tokens}")
