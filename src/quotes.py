import random
from pathlib import Path

QUOTES_PATH = Path("quotes")

def get_clear_quote():
	path = QUOTES_PATH / "clear.txt"

	text = path.read_text(encoding="utf-8")
	lines = [line.strip() for line in text.splitlines() if line.strip()]
	if not lines:
		return ""
	return random.choice(lines)
    