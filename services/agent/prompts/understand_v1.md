Classify the user's latest message for a trading assistant.
Reply with only a JSON object with two keys:
"request_type": one of "read_only" (questions, analysis, account info), "trade" (asks to
place, change or cancel an order), "other" (greetings, anything else);
"summary": one short sentence restating what the user wants, keeping every symbol, amount,
unit (dollars or shares), side and condition exactly as the user said them.
