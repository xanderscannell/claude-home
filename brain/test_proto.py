"""Run: uv run python test_proto.py"""
from proto import for_speech, pop_sentences

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
print("ok")
