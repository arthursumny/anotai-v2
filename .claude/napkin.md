# Napkin Runbook

## Execution & Validation
1. **[2026-09-24] Preserve backend contracts while improving product interactions**
   Do instead: keep routes, authentication, and API payloads stable while adding explicitly requested controls such as filters or drag and drop.
2. **[2026-09-24] Cover responsive behavior across the full web app**
   Do instead: inspect landing, navigation, forms, filters, cards, dialogs, and dense content at mobile, tablet, and desktop widths.
3. **[2026-10-01] Include dependencies imported during API startup**
   Do instead: inspect imports reached from `app.main` and list optional transports such as `requests` in `requirements.txt` so an integration route cannot prevent Uvicorn from starting.

## Product UX
1. **[2026-09-24] Explain Anotaí by user outcomes**
   Do instead: lead with finding ideas, remembering commitments, and following up; keep model and protocol names out of the main landing message.
