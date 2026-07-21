# Test environment must exist before product_analyzer is imported —
# the module exits at import time when API_KEY is missing.
import os

os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("ENABLE_PLAYWRIGHT", "false")
# Without GOOGLE_API_KEY the Vision client falls back to default
# credentials and cleanly degrades to None in test environments.
os.environ.pop("GOOGLE_API_KEY", None)
