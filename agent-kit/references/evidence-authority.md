# Evidence authority and source binding

Load this reference when native text, page pixels, OCR, table topology, parser
facts, or resolver suggestions conflict.

1. Source identity and authorization are Host facts and cannot be overridden.
2. Visible page evidence governs visible wording, marks, numbering, layout, and
   content that the native layer omits or corrupts.
3. Native text/table facts are preferred for exact searchable content when the
   page ledger declares them complete and they agree with visible evidence.
4. OCR is an observation with confidence and bounding evidence, not repaired
   source truth.
5. Parser facts and resolver suggestions are queryable candidates. They may
   locate evidence or propose continuity but cannot veto a grounded Agent
   proposal or define its universe.
6. Every cross-page or cross-table relationship needs explicit endpoint
   evidence. Proximity, numbering similarity, or repeated column counts alone
   are insufficient.
7. Every proposal uses source identities copied from the exact current bundle.
   Never construct or remember a handle across runs. The Host resolves handles,
   cells, tables, assets, placements, and pages to locators/hashes and rejects
   every missing, stale, overlapping, or drifting reference.
