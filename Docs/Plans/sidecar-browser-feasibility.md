# Isolated browser feasibility gate (not implementation approval)

**Decision:** spike before browser/tool promises. Current Rust sidecar has one trusted offline Wry WebView, custom `sidecar://` protocol, navigation restricted to its origin, and parent-pipe view/snapshot messages. There is **no** external page load, browser automation, screenshot, tab isolation, artifact viewer, or proof that hostile pages cannot reach the trusted shell.

## Windows/WebView2 spike

1. Create a second, untrusted WebView with its own profile/data directory and **no** shell IPC, `SidecarShell`, privileged custom protocol or file access. Do not weaken trusted shell navigation policy. Serve a local hostile page and inspect renderer/profile/process separation.
2. Measure Wry/WebView2 APIs for navigate, visible text result, screenshot pixels, click/type/scroll and tab IDs; demonstrate actual responses, not merely successful JS injection. Test wrong tab, closed tab, concurrent navigation, timeout and cancellation.
3. Observe/deny redirect, subresource, popup, download, permission and local-file attempts. Try hostile JS calling shell APIs, parent IPC and `sidecar://` assets. Test oversized/malformed responses and a hung/crashed page. Any boundary escape blocks shipment.
4. Open two untrusted tabs with independently targeted actions/state. Verify session/cookie sharing policy explicitly and cleanup after close/reopen.

Windows success does not establish macOS/Linux parity: repeat platform-specific WebView, screenshot, profile, popup and input tests before claiming support. Keep Chrome bridge unchanged; if Wry cannot supply secure useful action parity, return measured gaps/options to Ryan before substituting a weaker browser.
