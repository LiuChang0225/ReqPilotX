# Vendor notice

The retained upstream plugin manifests and ReqPilotX provenance records declare
the OpenAI primary-runtime PDF capability under the MIT license. The snapshots
available to this repository did not include a standalone upstream license-text
file, so R5 does not invent one or claim that a reconstructed text is the
upstream artifact. The license declaration, author, source URL, distribution
version and file hashes remain in each release's provenance JSON.

ReqPilotX has not patched the three vendored skill/runtime files. If an upstream
release changes those bytes, add a new versioned directory and record the new
manifest/provenance instead of overwriting an existing snapshot.
