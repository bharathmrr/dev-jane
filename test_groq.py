"""Quick test — Groq API hello world."""
import os
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("GROQ_API_KEY", "")
print(f"API key present: {bool(api_key)}")
print(f"Key starts with: {api_key[:20]}...")

from groq import Groq

client = Groq(api_key=api_key)

resp = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[{"role": "user", "content": "Say exactly: Hello World from Groq!"}],
    max_tokens=20,
    temperature=0,
)

print(f"\nGroq says: {resp.choices[0].message.content.strip()}")
print(f"Model used: {resp.model}")
