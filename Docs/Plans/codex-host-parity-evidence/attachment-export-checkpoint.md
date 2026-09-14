# C3 embedded attachment export checkpoint

Conversation export now copies embedded PNG/JPEG/WebP/GIF content blocks to an
exclusive sibling assets directory, with content-hash filenames and relative
Markdown links. Matching content is deduplicated. Conversation bodies refer to
the corresponding asset, while the attachment section renders the image link.
No base64 bytes appear in Markdown. Remote attachments are never fetched.

Validation: 12 export tests pass. Added byte-for-byte attachment comparison,
duplicate-image reuse, relative links with spaces, source preservation, invalid
base64 rejection before output creation, and pre-existing assets-folder protection
without leaving a partial document. Scoped Ruff passes. This adds no model calls.

Source formats outside the supported embedded image types remain explicit failures;
remote attachments remain placeholders. Packaged client export and the remaining
cross-client/runtime/full-plan gates are still open.
