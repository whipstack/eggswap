# Codex multi-bucket rate-limit protocol audit

Date: 2026-09-23. Scope: one targeted question — does the current adapter
preserve all documented Codex rate-limit buckets and sparse updates?

## Protocol evidence

Primary source: OpenAI Codex App Server protocol at commit
[`ce7df362375772367bbba3deb9fb41632e96be1a`](https://github.com/openai/codex/tree/ce7df362375772367bbba3deb9fb41632e96be1a),
`codex-rs/app-server-protocol/src/protocol/v2/account.rs` and the generated
`GetAccountRateLimitsResponse.json` schema.

- A full read has a backward-compatible `rateLimits` snapshot and an optional
  `rateLimitsByLimitId` map. The map is the complete set of metered limit IDs.
- Each snapshot can have `primary`, `secondary`, `credits`, and an optional
  `normalModelSlug`. Window duration and reset time are optional; an absent
  value is not zero or a made-up default.
- `account/rateLimits/updated` carries a sparse `RateLimitSnapshot`. The
  protocol says to merge available values into the latest full read or refetch
  it. Nullable metadata unavailable in the update does not erase the prior
  observed value.

This is a protocol contract, not proof that this installation emits a push.

## Live evidence

The installed Codex CLI was 0.156.1 for the redacted sample in
[`codex-ratelimits-live.md`](codex-ratelimits-live.md). That read contained one
`codex` primary window, `secondary: null`, and a duplicate one-entry
`rateLimitsByLimitId` map. The extended 150-second push probe did not observe
`account/rateLimits/updated`; the event remains UNKNOWN on this installation.
It has not been re-probed as part of this change.

## Candidate behavior

The reader now prefers the map while retaining the legacy single-bucket view,
preserves all primary/secondary windows, and stamps them at local reply
arrival. A window with an explicit `normalModelSlug` remains visible in the
diagnostic window list. It binds the scheduling result only when the caller
requests that exact model; with no model requested, scoped-only data yields
UNKNOWN, and scoped buckets do not make an otherwise measured account-wide
bucket look exhausted. This exact model-string match follows the protocol's
description of `normalModelSlug`; whether every provider alias matches the
caller's model string is an inference and remains unmeasured.

`merge_rate_limit_update` merges one named meter and its non-null window
fields without replacing other meters. `AppServerRateLimitReader` exposes
`apply_notification` for a caller that already owns a notification stream.
The standard reader is one-shot: it does not observe or claim pushes arriving
after its read reply. The merge path has disposable-fixture coverage; no live
limit change was induced.

## Status

Code and fixture behavior are a candidate on `codex/eggswap-codex-telemetry`.
Multi-bucket live payload behavior, the push event on this installation, and
the provider's exact model-to-meter mapping are UNKNOWN. This audit does not
close #1094 or claim installed production reachability.
