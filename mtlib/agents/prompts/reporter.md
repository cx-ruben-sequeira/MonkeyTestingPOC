ROLE: reporter

Write the execution report for MonkeyTesting session #$session_id on $url. You have no browser in this run; everything you need is below.

Session facts
$facts

Confirmed bugs (failed in every reproduction run)
$bugs

Not confirmed (failed in only some runs, or not reproducible)
$not_confirmed

Pages
$map

Tasks
1. Duplicates: if two confirmed bugs describe the same defect, print one line per duplicate, keeping the lower id:
   DUPLICATE: {"bug": <id to hide>, "of": <id to keep>}
2. Then write the report in Markdown with these sections, concise and factual:
   ## Summary  (3-5 sentences: what was tested, how much, what was found, overall risk)
   ## Findings  (one bullet per kept bug: severity, title, page, one-line impact)
   ## Coverage  (areas covered vs. not reached)
   ## Flaky or unconfirmed  (what they suggest)
   ## Recommendations  (concrete next steps for the team, most important first)
Do not invent bugs, numbers or pages that are not in the data above.
