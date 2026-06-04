import sys
import os
from datetime import datetime

# Ensure root directory is in python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.email_generator import analyze_reply_intent
from app.core.config import settings

def test_intent_parsing():
    print("--- Testing analyze_reply_intent ---")
    print(f"Groq API Key status: {'Configured' if settings.GROQ_API_KEY else 'NOT CONFIGURED (Heuristic fallback only)'}")
    
    # We will simulate today is Monday, Jan 5, 2026
    today_str = "Monday, 05 January 2026"
    print(f"Simulating Today's date: {today_str}\n")
    
    test_cases = [
        {
            "name": "Decline meeting",
            "reply": "No thank you, I'm not interested in this."
        },
        {
            "name": "General week preference - this week",
            "reply": "Hi Leo, this week works best for me. Let me know when."
        },
        {
            "name": "General week preference - next week",
            "reply": "Can we do next week instead?"
        },
        {
            "name": "Specific date & time booking",
            "reply": "Hi Leo, let's meet on Tuesday, Jan 6 at 2 PM IST. Does that work?"
        },
        {
            "name": "Show slots after Jan 8",
            "reply": "Hi, please show me the slots after Jan 8."
        },
        {
            "name": "Show slots after Wednesday",
            "reply": "Could you send options for any time after Wednesday?"
        },
        {
            "name": "Unclear positive response",
            "reply": "Sounds great, let's schedule a call."
        }
    ]
    
    for tc in test_cases:
        print(f"CASE: {tc['name']}")
        print(f"  Reply Body: \"{tc['reply']}\"")
        res = analyze_reply_intent(tc["reply"], today_str)
        print(f"  Parsed Result: {res}")
        print("-" * 50)

if __name__ == "__main__":
    test_intent_parsing()
