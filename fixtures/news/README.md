# News fixtures: all synthetic

Every file in this folder is **synthetic test data** written for TradeGuard's replay
simulator and evaluations. None of it is real news, and none of it describes real events,
statements or filings by the companies named. Source names ("Daily Market Brief", "Trader
Notes Blog", ...) are fictional.

Each item carries its labels in metadata, not in the text the agent reads (DECISIONS.md D013):

| Field | Meaning | Shown to the agent |
|---|---|---|
| `synthetic` | always `true` | no |
| `trusted` | always `false` (external free text) | no |
| `injection` | the item contains a prompt injection payload (eval label) | no |
| `eval_notes` | what the payload tries to make the agent do | no |
