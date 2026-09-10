# Changelog

## [1.2.0] - 2026-08-25
### Added
- Two functional eval scenarios covering the corrected knowledge: TGW→VPN summarization (keep propagation + static summary + limit-hit selection order) and overlapping DX supernet into Cloud WAN (Routing Policies drop/suppress on DX; no BGP communities on DX attachments).
- Matching positive trigger queries for the two new scenarios.
### Changed
- `references/tgw-routing-patterns.md`: clarified the limit-hit advertisement-selection order (static over propagated, then least-specific), labeled as field knowledge.
- `references/cloudwan-dx-routing-patterns.md`: added that Cloud WAN Routing Policies support prefix filtering (drop/summarize) inbound/outbound on Direct Connect attachments (distinct from the legacy allowed-prefixes list); DX attachments cannot match/set BGP communities.

## [1.1.0] - 2026-08-18
### Changed
- Refined the `description` for better activation: added symptom-based trigger phrasings and explicit
  service/keyword coverage.
### Added
- Expanded functional evals (DX location preference, TGW ECMP, DX+VPN redundancy, verification of
  unknown sources) — 7 scenarios total.
- Expanded trigger tests with more positive routing prompts and additional negative (non-routing)
  prompts.

## [1.0.0] - 2026-08-18
### Added
- Initial release of the `aws-routing` skill, adapted from the AWS Routing custom agent.
- Route-evaluation guidance for Cloud WAN CNEs, Direct Connect Gateway path selection, Transit
  Gateway route tables, and VPC route tables.
- BGP traffic-engineering guidance: LP communities (`7224:7100/7200/7300`), AS-path prepending
  (within-region only), MED (low-priority tiebreaker), and longest-prefix-match behavior.
- DX + VPN redundancy, active/active vs active/passive, and failover analysis.
- Reference knowledge base under `references/`.
- Verification / anti-hallucination directives built into the skill instructions.
