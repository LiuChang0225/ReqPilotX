from __future__ import annotations

# Prefer the OS trust store so Azure HTTPS calls survive enterprise TLS interception
# on Windows developer machines.
try:
	import truststore
except Exception:
	truststore = None
else:
	truststore.inject_into_ssl()
