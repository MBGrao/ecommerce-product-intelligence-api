---
name: Bug report
about: Something returns wrong data, errors, or crashes
title: ""
labels: bug
assignees: ""
---

## What happened

<!-- Actual behaviour, including the full error/response body if any. -->

## Expected behaviour

## Reproduction

<!-- Endpoint called, request payload (redact your API key!), and the
     product URL involved if the issue is scraping-related. -->

```bash
curl -X POST "http://localhost:8000/analyze/partial" \
  -H "X-API-Key: <redacted>" \
  -H "Content-Type: application/json" \
  -d '{ ... }'
```

## Environment

- Deployment: local / VPS / other
- Python version:
- Playwright enabled (`USE_PLAYWRIGHT`):
