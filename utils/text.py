"""
utils/text.py — shared text helpers.

display_title():
  Class "name" (jo generate karte time daala jaata hai) URL-safe slug hota hai
  (spaces -> hyphens, sirf letters/numbers/hyphen). Lekin jahan bhi ye title
  DIKHANA ho (player page header, Telegram caption) — wahan hyphen(-) aur
  underscore(_) ko wapas simple SPACE se replace karke clean readable title
  dikhana hai. Ye function hi single source of truth hai taaki har jagah
  (Flask templates + recorder.py + Telegram bot) exact same conversion ho.
"""
import re


def display_title(name: str) -> str:
    if not name:
        return ""
    # hyphen aur underscore (ek ya zyada continuous) -> single space
    title = re.sub(r"[-_]+", " ", str(name))
    title = re.sub(r"\s+", " ", title).strip()
    return title
