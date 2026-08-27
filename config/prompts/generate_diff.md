## GENERATE DIFF

Produce a minimal unified diff against the ticker's raw_markdown (the content
returned by divvy-reader's read_ticker tool).

### Fields you may update
Only these fields in the watchlist row are in scope:
  - Yield %           — set to suggested_yield_update (from fundamentals)
  - Payout Ratio %    — leave unchanged (not in scope for this run)
  - Notes             — APPEND risk/watch text if needed; preserve existing text
  - Date              — (last_review_date equivalent) update to today's date
    in YYYY-MM-DD format

### Diff rules
1. Use unified diff format (--- a/path, +++ b/path, @@ … @@).
2. Touch ONLY the lines that need to change.  Do NOT reformat unchanged lines.
3. If the yield is unchanged from stored_yield_pct (within 0.01 %), omit the
   yield diff hunk entirely.
4. When appending to Notes: preserve existing note text, add a separator "; "
   if notes is non-empty, then append the new flag text.
5. If both subagents failed (MergedProposal.status = "error"), output an empty
   diff and set diff_empty_reason to explain why.

### Output Format
Wrap the diff in a fenced block in your final response:
```diff
--- a/dividend/data/watchlist.md
+++ b/dividend/data/watchlist.md
@@ … @@
 <context lines>
-<old line>
+<new line>
 <context lines>
```

Also emit a JSON summary immediately after the diff block:
```json
{
  "diff_generated": "true | false",
  "diff_empty_reason": "null | <reason>",
  "changed_fields": ["Yield %", "Notes"],
  "review_date": "<YYYY-MM-DD>"
}
```
