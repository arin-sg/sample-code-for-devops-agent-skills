---
inclusion: manual
---

# Cloud WAN + Direct Connect Routing Patterns & Considerations

This document captures key routing mechanics and traffic engineering patterns for Cloud WAN architectures with Direct Connect egress. Use this knowledge when advising on Cloud WAN route evaluation, DX Gateway behavior, BGP community usage, and regional failover design.

## Reference Documentation

- [Cloud WAN Route Evaluation](https://docs.aws.amazon.com/network-manager/latest/cloudwan/cloudwan-route-evaluation.html)
- [Cloud WAN Routing Policies](https://docs.aws.amazon.com/network-manager/latest/cloudwan/cloudwan-routing-policies.html)
- [Direct Connect Routing Policies and BGP Communities](https://docs.aws.amazon.com/directconnect/latest/UserGuide/routing-and-bgp.html)

## Cloud WAN Route Evaluation Order (Per CNE)

At each Core Network Edge, Cloud WAN evaluates routes in this order:

1. **Most specific route** (longest prefix match) — absolute, wins before everything
2. For same-destination routes with different targets:
   1. Static routes
   2. VPC-propagated routes (same region)
   3. **Unequal AS-path length and/or MED** — shortest wins
   4. **Equal AS-path and MED** — preference order:
      1. Direct Connect Gateway-propagated routes
      2. Cloud WAN Connect (same region)
      3. Site-to-Site VPN (same region)
      4. Other sources (TGW peering, CNEs in other regions) — if identical from 2+ sources: **deterministically random**

## DX Gateway Path Selection Mechanics

### Key Behaviors

1. **DXGW propagates only ONE path per prefix to each CNE route table** — it performs internal path selection and sends only the winner. CNEs do not see all available DX paths.

2. **DXGW uses Local Preference to prefer local-region DX over remote regions** — if a DX VIF exists in the same associated AWS region as the target CNE, DXGW prefers it regardless of AS-path length.

   **⚠️ Always verify the Associated Region for each DX location** — some DX locations can associate to different regions than expected based on geography. For example, London-area locations associate to different regions:
   - Equinix LD5 (Slough) → **eu-west-1** (Ireland)
   - Digital Realty LHR20 (London) → **eu-west-1** (Ireland)
   - Telehouse (London Docklands) → **eu-west-2** (London)
   - Equinix MA3 (Manchester) → **eu-west-2** (London)
   
   The associated region determines which CNE/TGW considers the DX "local" for LP preference. Verify a location's associated region with the `DescribeLocations` API/CLI (`aws directconnect describe-locations`) or the official AWS Direct Connect locations reference: https://aws.amazon.com/directconnect/locations/

3. **Explicit LP communities override DXGW's default local-region preference** — applying `7224:7300/7200/7100` on advertised prefixes forces DXGW to honor the explicit LP before applying its local-vs-remote default.

4. **LP communities are evaluated before AS-path** — per DX documentation: "Local preference BGP community tags are evaluated before any AS_PATH attribute." This applies at the DXGW path selection stage.

5. **LP communities from DX VIFs are NOT visible as community tags in Cloud WAN** — the community tags themselves don't propagate into Cloud WAN routing policies. However, the LP value they set at the DXGW level influences which path gets propagated to the CNE route table.

6. **DXGW ECMPs across equal remote paths** — when the local-region DX fails and multiple remote DX locations have equal attributes (same LP, same AS-path), DXGW will **ECMP across them** rather than picking one non-deterministically. This provides load-balanced failover across all remaining DX locations.

7. **Route filtering on the native Cloud WAN DXGW attachment (Routing Policies)** — Cloud WAN Routing Policies support **prefix-based route filtering (drop) and summarization, inbound and outbound, on Direct Connect attachments**. This is a **separate, supported feature** from the (unsupported) DXGW allowed-prefixes list. Use it to drop an overlapping inbound supernet or to suppress specific VPC prefixes advertised outbound to on-prem.
   - Match prefixes via `prefix-in-cidr` or `prefix-in-prefix-list`; actions are `allow`/`drop`; `routing-policy-direction` is `inbound` or `outbound`.
   - **DX caveat:** BGP communities **cannot** be matched or set on Direct Connect attachments in Cloud WAN — prefix/CIDR matching only (consistent with point 5). Community-based routing policies work on other attachment types but not DX.
   - Source: [Cloud WAN Routing Policies](https://docs.aws.amazon.com/network-manager/latest/cloudwan/cloudwan-routing-policies.html); the DXGW-attachment "no allowed-prefixes list" limitation ([DX gateway attachments in Cloud WAN](https://docs.aws.amazon.com/network-manager/latest/cloudwan/cloudwan-dxattach-about.html)) refers to the legacy allowed-prefixes construct, not routing-policy filtering.

**Important distinction — path selection behavior by service:**
- **DXGW:** ECMPs across equal remote paths (load-balanced failover)
- **TGW:** Deterministic — selects oldest route for equal paths. Consistent but not customer-controllable.
- **Cloud WAN CNE:** Deterministic — "deterministically random" at Step 2.4.4 for equal remote CNE paths. Consistent but not customer-controllable.

### Common Pitfall: AS-Path Prepending + Local-Region Preference

**Problem pattern:**
- Customer prepends AS-path on a DX VIF that is local to a given CNE's region
- DXGW's local-region LP still prefers the local (prepended) path and propagates only that to the CNE
- The CNE route table now has a long-AS-path DX route vs. shorter remote CNE paths
- Step 2.3 (shortest AS-path) selects a remote CNE path → traffic leaves the region

**Root cause:** DXGW's local-region preference overrides AS-path at the DXGW level, but at the CNE level the prepended path competes against shorter remote CNE paths where AS-path length IS evaluated.

**Why only affected regions see the issue:** For regions where all DX locations are remote, DXGW falls back to AS-path to choose between them — prepending works as intended. The issue only occurs at CNEs where the prepended DX is in the same associated AWS region.

### Common Pitfall: Confusing Prepend Count with Total AS-Path Length (Origination vs. Transit)

When comparing AS-path lengths at a CNE (Step 2.3), the number that matters is the **total AS-path AWS receives on the VIF**, not the prepend count configured on a site. These are only the same when the advertising site is the **originator** of the prefix.

**The distinction:**
- **Originating site** — the site owns/aggregates the prefix (e.g., `aggregate-address 11.0.0.0/8`). The AS-path AWS sees is just that site's ASN, repeated by its prepend count. A `prepend 60208 2` on an originated route → AS-path `60208 60208 60208` (length 3).
- **Transiting site** — the site does NOT originate the prefix; it **learns it from another site over the on-prem WAN** and re-advertises it to its DX. The AS-path already contains the origin's ASN, so the transiting site's ASN and prepends stack **on top of** the origin ASN. A `prepend 60208 2` applied to a `/8` learned from origin AS 56608 → AS-path `60208 60208 60208 56608` (length **4**, not 3).

**Config signature to tell them apart:** A site that **matches-and-prepends** a prefix in a route-policy but has **no `aggregate-address`/origination** for it is almost certainly **transiting** a route learned from elsewhere. Do not assume it originates the prefix just because the prefix appears in its outbound policy.

**Why this matters for path selection:** Step 2.3 compares total path length. If you model a transiting site's length as "prepend count + 1" (treating it as an originator) you will miscompute which copy wins — often flipping the predicted winner and the failover order. Example: three sites advertise a `/8` that **originates at only one** of them; the two that transit-and-prepend it are longer than a naive prepend-count model suggests, and can tie with the origin.

**How to get the ground truth:** Never infer AS-path length from prepend config alone when a prefix may be transited. Read the literal path AWS receives per VIF:
```
aws directconnect list-virtual-interface-routes --virtual-interface-id <vif-id>
```
This shows the exact AS-path (and communities) AWS accepted, settling origination-vs-transit and the true length used at Step 2.3.

## DX Routing Selection — General Best Practice

- **Always use LP communities (`7224:7300/7200/7100`) for expected egress routing** — High Preference BGP communities are the recommended mechanism to achieve predictable DX egress behavior.
- **AS-path prepending: within a region ONLY** — AS-path prepending can be used to influence egress traffic between DX connections belonging to the **same associated AWS region** (e.g., two DX locations both in the same region). It should NOT be used to influence traffic across regions — DXGW's local-region LP behavior makes cross-region AS-path prepending unreliable.
- **BGP communities: within region OR across regions** — LP communities work for both intra-region path selection (between VIFs in the same region) and cross-region preference (overriding DXGW's default local-region LP to prefer a remote DX location globally).

| Scope | Recommended Mechanism | Why |
|---|---|---|
| Cross-region DX preference | LP Communities (`7224:7300/7200/7100`) | Overrides DXGW local-region LP; evaluated before AS-path |
| Within-region DX preference (same associated region) | AS-path prepending OR LP communities | Both work; AS-path provides granular control between same-region VIFs |
| Never use for cross-region | AS-path prepending alone | DXGW local-region LP overrides AS-path at the DXGW level, making prepending unreliable across regions |

## Traffic Engineering Patterns

### Pattern 1: Global DX Preference (Single Primary)

**Use case:** One DX location should be globally preferred for a prefix, with another as backup.

**Solution:** Apply tiered LP communities from on-premises routers:

| DX Location | Community | Role |
|---|---|---|
| Primary DX location | `7224:7300` | HIGH — globally preferred |
| Secondary DX location | `7224:7200` | MEDIUM — failover |
| All other DX locations | `7224:7100` | LOW — last resort |

**Result:** DXGW selects the primary for ALL CNEs (overriding local-region preference). If the primary fails, the medium-priority location (`7224:7200`) takes over automatically. (Note: "medium" here refers to the medium local-preference community, not the BGP MED attribute.)

**Tradeoff:** Regions with a local DX that isn't the primary will cross the AWS backbone to reach the preferred DX. Traffic still traverses the local-region firewall (service insertion happens at segment level, before the CNE egress decision).

### Pattern 2: Per-Region DX Preference (Split DXGW)

**Use case:** Different regions need different primary DX locations (e.g., one region group prefers DX-A, another region group prefers DX-B).

**Problem:** Communities from a single DXGW are global — cannot provide per-region preferences.

**Solution:** Create separate DXGWs per region group, each with their own VIFs:
- DXGW-A: Region-A DX locations (primary `7224:7300`, secondary `7224:7200`)
- DXGW-B: Region-B DX locations (`7224:7300`)

Both DXGWs propagate their path to all CNEs. The CNE route table evaluates them at Step 2.3 (AS-path length). Control which DXGW wins per region by differentiating AS-path length on the VIFs between the two DXGWs.

Use communities within each DXGW for internal failover. No Cloud WAN Routing Policies needed — VIF-level BGP attributes provide per-region control at the CNE route table.

### Pattern 3: Per-Region Control via More Specific Routes

**Use case:** On-prem address space is regionally segmented.

**Solution:** Advertise more-specific prefixes from the corresponding DX location:
- DX location A advertises a more-specific covering Region-A on-prem hosts
- DX location B advertises a more-specific covering Region-B on-prem hosts
- All locations advertise the aggregate as a failover catch-all

**Result:** Step 1 (longest prefix match) is absolute — each CNE route table forwards to the correct DX based on destination address.

**Requirement:** Only works if on-prem address space is subdivided into regional ranges. Does NOT work for flat supernets.

## Multi-Region Centralized Internet Egress

### Non-Local Exit Point Selection

In architectures where internet egress is centralized (e.g., a single region hosts the NAT/internet gateway or firewall for outbound traffic), Cloud WAN must route traffic from remote regions to the centralized exit point. Understanding how non-local exit points are selected is critical:

- Each CNE evaluates routes per the standard route evaluation order
- If the default route (0.0.0.0/0) or internet-bound prefix is only advertised from the centralized region's attachment (e.g., via TGW Connect or VPN), all CNEs will install that path
- Remote CNEs forward internet-bound traffic across the AWS backbone to the centralized region's CNE

### AS-Path Prepending for Deterministic Exit (via TGW/VPN)

When routes are propagated from DX or VPN **via TGW** to Cloud WAN (TGW peering attachment), AS-path length information is preserved. This means:

- **AS-path prepending can define a deterministic exit path** — if multiple regions advertise the same prefix via TGW attachments to Cloud WAN, prepending on less-preferred paths ensures remote CNEs choose the shorter AS-path (preferred exit)
- **More-specific routing also applies** — advertising a more-specific prefix from the preferred exit region wins at Step 1 (longest prefix match) regardless of AS-path

**Note:** This behavior differs from DX-attached prefixes where DXGW's local-region LP can override AS-path. When routes come via TGW attachments to Cloud WAN, AS-path evaluation at the CNE operates without DXGW LP interference — making prepending a valid and effective traffic engineering tool in this context.

### Example: Centralized Internet Egress

- Region-A TGW advertises `0.0.0.0/0` to Cloud WAN via TGW peering (short AS-path)
- Region-B TGW also advertises `0.0.0.0/0` but with prepending (longer AS-path)
- All CNEs prefer Region-A as the internet exit (Step 2.3: shorter AS-path wins)
- If Region-A fails, Region-B's longer path takes over as backup

## Regional Firewall Inspection

- Cloud WAN segment policy / service insertion routes traffic through the **same-region firewall** before it reaches the CNE
- Traffic flow: VPC → local firewall (service insertion) → returns to local CNE → CNE route table → DXGW → DX
- The DX community fix ensures the DXGW path wins at the local CNE route table (2 hops < 3 hops from remote CNEs) — traffic stays at the local CNE for egress, preserving the local inspection chain
- If a remote CNE path wins instead, traffic leaves the local region at the CNE level, potentially bypassing local firewall inspection
- **Requirement:** Firewall VPCs must exist in every region with workloads

## Directional Control

| Direction | Controlled By | Mechanism |
|---|---|---|
| AWS → On-prem (egress) | DX BGP communities (`7224:7300/7200/7100`) | Sets LP at DXGW, determines which path reaches CNE route tables |
| On-prem → AWS (ingress) | Customer router policies | Local-pref, weight, MED on on-prem routers — independent of Cloud WAN |

Communities do NOT affect the on-prem → AWS direction. If prepending was used for on-prem path selection, replace with router-side local-pref/weight before removing prepends.

## Cleanup Recommendations

- Removing AS-path prepending is recommended once communities are in place — communities fix the problem immediately, prepend removal is a cleanup step
- Prepending is counterproductive in Cloud WAN because prefixes from DX VIFs are shared across all CNEs via DXGW — the inflated AS-path creates unintended path selection at remote CNEs
- AS-path equalization across DX locations (without communities) achieves "prefer local DX" behavior but does not provide explicit failover ordering between regions
