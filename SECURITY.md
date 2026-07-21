# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately via
[GitHub private vulnerability reporting](https://github.com/MBGrao/ecommerce-product-intelligence-api/security/advisories/new)
rather than opening a public issue.

You can expect an initial response within a week. Please include
reproduction steps and the impact you believe the issue has.

## Scope notes for researchers

- The service fetches user-supplied URLs; the SSRF guard lives in
  `validate_public_url` / `is_private_host` in `product_analyzer.py`
  and is covered by `tests/test_url_safety.py`.
- All analysis endpoints require an `X-API-Key` header; the key is
  deployment-specific and never committed to this repository.
- Secrets are loaded from an untracked `.env` file (see `.env.example`).
  If you find a credential in the repository or its history, treat it
  as revoked — and please report it anyway.

## Supported versions

Only the latest `master` is supported.
