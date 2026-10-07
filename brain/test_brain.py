"""Run: uv run python test_brain.py"""
import os
import tempfile
import time

os.environ["CLAUDE_HOME_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")  # before importing tools

import tools
from brain import for_speech, pop_sentences

assert pop_sentences("Hi there. How are") == (["Hi there."], "How are")
assert pop_sentences("It costs 3.50 dollars! Okay") == (["It costs 3.50 dollars!"], "Okay")
assert pop_sentences("Really?! Yes. ") == (["Really?!", "Yes."], "")
assert pop_sentences('She said "stop." Then') == (['She said "stop."'], "Then")
assert pop_sentences("Line one\nLine two") == (["Line one"], "Line two")
assert pop_sentences("no end yet") == ([], "no end yet")
assert pop_sentences("Then Dr. Martin Luther King Jr. spoke. After") == (["Then Dr. Martin Luther King Jr. spoke."], "After")
assert pop_sentences("Bring snacks, e.g. chips. Ok") == (["Bring snacks, e.g. chips."], "Ok")
assert pop_sentences("Washington D.C. is the capital. It") == (["Washington D.C. is the capital."], "It")
assert pop_sentences("So did I. Then") == (["So did I."], "Then")  # "I." is not an initial
assert pop_sentences("Turn left on Main St. ") == ([], "Turn left on Main St. ")  # held until more text arrives
assert for_speech("Dr. Martin Luther King Jr. spoke.") == "Doctor Martin Luther King Junior spoke."
assert for_speech("Snacks, e.g. chips, etc.") == "Snacks, for example chips, et cetera"
assert for_speech("Drive to Mt. Hood vs. Mt. Rainier") == "Drive to Mount Hood versus Mount Rainier"
assert for_speech("Drs. and Mrs.Smith") == "Drs. and Mrs.Smith"  # only whole tokens

# Shopping list: no duplicates, case-insensitive removal, reports what wasn't there
tools.shopping_add(["Milk", "eggs", "milk"])
assert tools.shopping_items() == ["Milk", "eggs"]
assert tools.shopping_remove(["MILK", "bread"]) == ["bread"]
assert tools.shopping_items() == ["eggs"]

# Memory: forget matches any fact containing the text, case-insensitive
tools.remember("Sam's birthday is May 3")
tools.remember("The wifi password is on the fridge")
assert tools.forget("birthday") == ["Sam's birthday is May 3"]
assert tools.facts() == ["The wifi password is on the fridge"]

# Timers: per device, cancel by label or all, due ones are popped once
tools.timer_add("kitchen", "pasta", 600)
tools.timer_add("kitchen", "eggs", 0)
tools.timer_add("office", "", 300)
assert [label for label, _ in tools.timers("kitchen")] == ["eggs", "pasta"]
assert tools.timers_due() == [("kitchen", "eggs")] and tools.timers_due() == []
assert tools.timer_cancel("kitchen", "PAS") == 1 and tools.timers("kitchen") == []
assert tools.timer_cancel("office", "") == 1
assert tools.spoken_duration(3725) == "1 hour 2 minutes 5 seconds"
assert tools.spoken_duration(60) == "1 minute"
print("ok")
