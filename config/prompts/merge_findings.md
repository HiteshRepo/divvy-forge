## MERGE FINDINGS

You now have the outputs of both subagents.  Combine them into a single
MergedProposal following these rules:

### If BOTH subagents succeeded (status = "ok" / risk_level ≠ "error"):

1. Determine the overall proposed action:
   - STRONG CHANGE: if fundamentals are deteriorating AND risk is high →
     recommend position-size reduction in notes.
   - WATCH FLAG: if fundamentals are safe BUT risk is high (conflicting
     signals) → add a risk-watch flag to notes while keeping yield update.
   - ROUTINE UPDATE: if risk is low/medium AND fundamentals are stable/
     improving → update yield and payout flag, no special note.

2. Write merge_reasoning that:
   - Cites both subagents' key numbers.
   - Explicitly addresses any conflict between payout_sustainability and
     risk_level (e.g. "fundamentals look safe at 42 % payout ratio, but a
     high-risk signal was found — flagging for human review").
   - Is at least two sentences long.

### If ONE subagent failed (status = "error"):

- Proceed with the successful subagent's findings only.
- Set merge_reasoning to acknowledge the failure: which subagent failed,
  what was used instead, and that the analysis is therefore partial.
- Mark the failed subagent's fields as null in the proposal.

### If BOTH subagents failed:

- Set status = "error" in the MergedProposal.
- Set merge_reasoning to explain that both subagents failed and no diff
  can be generated.
- Do NOT proceed to GENERATE DIFF.

### MergedProposal schema
```json
{
  "ticker": "<ticker>",
  "status": "ok | error",
  "merge_reasoning": "<≥2-sentence narrative>",
  "fundamentals": "<FundamentalsFindings or null>",
  "risk": "<RiskAssessment or null>",
  "error_detail": "null | <summary of what failed>"
}
```
