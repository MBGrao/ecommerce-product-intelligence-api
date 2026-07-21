# Contributing

Thanks for your interest in improving the Product Intelligence API.

## Development setup

```bash
git clone https://github.com/MBGrao/ecommerce-product-intelligence-api.git
cd ecommerce-product-intelligence-api
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt pytest
```

`API_KEY` is required at import time; the test suite sets a dummy value
automatically, so running tests needs no configuration:

```bash
pytest -q
```

Google Vision credentials and Playwright browsers are **not** needed for
the tests — the Vision client degrades to `None` and Playwright is
disabled via `ENABLE_PLAYWRIGHT=false`.

## Making changes

1. Open an issue first for anything larger than a small fix, so we can
   discuss the approach.
2. Create a feature branch off `master`.
3. Keep changes focused — one topic per pull request.
4. Add or update tests for any changed behaviour. CI must pass.
5. Follow the commit style used in the repo:

   ```
   type(scope): concise description

   fix(validation): reject malformed product identifiers
   test(api): cover expired-session response
   docs(setup): clarify configuration
   ```

## Pull requests

Fill in the PR template — especially **testing performed** and
**security impact** (this service fetches user-supplied URLs, so any
change near `validate_public_url` / `is_private_host` gets extra
scrutiny).

## Security issues

Please do **not** open public issues for vulnerabilities — see
[SECURITY.md](SECURITY.md).
