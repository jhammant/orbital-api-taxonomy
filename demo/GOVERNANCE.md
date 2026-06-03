# Governance — gating The Company Brain for a regulated buyer

> **Status: written deliverable only.** Nothing in this document has been applied
> to the running stack. It does **not** change `orbital-dev/config/roles.conf`,
> any `*.taxi` model, or the live schema. The sample policy below is a **proposal
> you would paste in**, not a file the loader is reading. The figures (a real
> attendee email, a real controller name) are quoted from the live services so the
> control surface is concrete, but the controls themselves are not yet switched on.

## Why a regulated buyer asks for this

The brain is, by design, a *federation* of public registers and a few internal
mocks. Several of the fields it returns are **personal data** about identifiable
people — company controllers, directors, meeting attendees. A bank, insurer, or
public body buying this as a KYB / portfolio-monitoring tool sits under UK GDPR,
the FCA's data-minimisation expectations, and their own internal "need to know"
controls. Their security review will not ask *"can it find the data"* — the demo
already proves that — it will ask:

> *Who is allowed to see a named person's risk profile, and where is the proof
> that an unauthorised caller did not?*

Orbital answers both with capabilities **already present in this stack**:

1. **Per-type access policies** — declarative rules attached to a semantic type
   that `permit`, `filter`, or **mask** values depending on the caller's role.
2. **Role-based access control** — the `roles.conf` already mounted into the
   container is where those roles are defined and bound to users / API clients.
3. **The lineage graph** — Orbital already records, per query, every upstream
   call and every field it flowed into. That *is* the audit trail; no extra
   instrumentation is needed.

Crucially, **the policy engine is already enabled.** `orbital-dev/docker-compose.yml`
launches Orbital with:

```text
--vyne.toggles.policiesEnabled=true
```

So enabling governance here is a matter of (a) declaring policies in the schema
and (b) adding the gating roles to `roles.conf` — not a platform change.

---

## 1. Which fields are PII (the data map)

These are the fields in the **current** models that identify a natural person, or
are derived from one. A regulated buyer needs each of these gated. Paths are given
so you can see exactly where the personal data enters the graph.

| Field (model · file) | Semantic type | Why it is PII | Real value seen live |
| --- | --- | --- | --- |
| `controllerRisk` — `InsolvencyProfile` · `insolvency.taxi` | `brain.insolvency.ControllerRisk` | **Names a specific person** and profiles their behaviour ("runs N other distressed companies"). The serial-failure match is computed on **canonical name + DOB year/month** (`controller_risk()` in `insolvency_service.py`), i.e. it is special-category-adjacent profiling of an identified individual. | `controller Ms Mary Patricia Kelleher runs no other distressed companies on record` (company `04458210`) |
| `email` — `MeetingAttendee` · `personal.taxi` | `brain.EmailAddress` | Direct personal identifier (work email of a named human). | `priya.sharma@monzo.com` |
| `name` — `MeetingAttendee` · `personal.taxi` | `brain.PersonName` | A named individual attending the caller's meeting. | `Priya Sharma`, `Tom Wright`, `Aisha Khan` |
| *(PSC controllers, upstream of `controllerRisk`)* | sourced as `name`, `dob_year`, `dob_month` in `psc_records` (`insolvency_service.py`) | Persons with Significant Control are **named individuals with a date of birth**. The DOB is used only to disambiguate the network and is **not** currently surfaced as its own typed field — keeping it un-exposed is itself a minimisation control to preserve. | (DOB is matched internally; not returned) |

Not PII, for contrast (so the policy stays tightly scoped): `companyName`,
`companyNumber`, `companyStatus`, registered `postcode` / `addressLine` (a company's
public registered office, not a home address), insolvency `stage` / `latestEvent` /
`eventDate`, and the markets / crime / flood / weather facts. These are public
register or environmental data and should stay readable by every role — masking
them would break the demo's whole "background any company" value with no privacy
benefit.

> **A note on the narrative layer.** `brain.NarrativeText` (`RiskNarrative.summary`,
> `llm.taxi`) is *generated prose* that can quote the controller signal. A complete
> deployment should treat it as a **derived PII surface** and apply the same role
> gate (the LLM prompt should be fed the masked value, so even the generated
> sentence never names the person for a non-Compliance caller). Called out here so
> the gate is applied at the source type, not bolted on after generation.

---

## 2. The control: per-type access policy with PII masking

Orbital lets you attach a `policy` to a **type**. Every query that would return a
value of that type is run through the policy first, so the control is enforced *at
the router*, uniformly, regardless of which query shape or which downstream service
produced the value. You cannot forget to apply it on one endpoint — it travels with
the type.

The rule we want for a regulated buyer:

> Mask any value that names or profiles a natural person **unless** the caller
> holds the `Compliance` role. A `Compliance` caller sees the real value; everyone
> else sees a redaction token and can still see the *non-personal* risk facts
> (stage, event, contracts) around it.

### Sample policy snippet (proposed — paste into a NEW schema file)

This is a **safe, non-applied sample.** Do **not** edit the live `insolvency.taxi`
/ `personal.taxi`. To trial it, drop it into a brand-new file in the workspace
(e.g. `orbital-dev/workspace/src/governance-policies.taxi`) so the live models stay
untouched and the policy can be reviewed / removed independently.

```taxi
import brain.insolvency.ControllerRisk
import brain.EmailAddress
import brain.PersonName

// Gate the person-naming serial-failure signal.
// Compliance officers see the real controller note; everyone else gets a
// redaction token. The surrounding non-PII fields (stage, event, contracts)
// are unaffected — they have no policy and flow normally.
policy ControllerRiskPolicy against brain.insolvency.ControllerRisk {
   read {
      case caller.roles contains "Compliance" -> permit
      else -> filterAll()   // value replaced with a redaction marker for other roles
   }
}

// Gate attendee email — direct personal identifier.
policy AttendeeEmailPolicy against brain.EmailAddress {
   read {
      case caller.roles contains "Compliance" -> permit
      else -> filterAll()
   }
}

// Gate attendee name — a named individual.
policy AttendeeNamePolicy against brain.PersonName {
   read {
      case caller.roles contains "Compliance" -> permit
      else -> filterAll()
   }
}
```

**How to read it.** A `policy ... against <Type>` block runs on every read of that
type. `case caller.roles contains "Compliance" -> permit` returns the real value
to a compliance officer; the `else` branch masks it for all other roles. Because
the policy is bound to the **type**, the same gate covers `controllerRisk` whether
it is fetched by `company-360`, the insolvency dossier, or any future query — there
is no per-endpoint wiring to keep in sync.

> Orbital's policy DSL evolves across releases (the masking primitive may surface as
> `filterAll()`, a `mask(...)` instruction, or a `permit`/`deny` pair depending on
> the build). The **shape** above — *per-type block, role predicate, permit-vs-mask
> branches* — is the contract that matters for the security review; pin the exact
> primitive to whatever the running image documents under `policiesEnabled`. The
> point this deliverable makes is that the gate is **declarative, type-scoped, and
> already switchable on this stack** — not that one keyword is final.

### Expected behaviour after applying it

Same query (`given { id : CompanyRegistrationNumber = "04458210" } find { InsolvencyProfile }`),
two different callers:

```json
// Caller WITHOUT the Compliance role  (e.g. an Analyst)
{
  "companyNumber": "04458210",
  "companyName": "…",
  "stage": "…",
  "latestEvent": "…",
  "controllerRisk": "*****"
}

// Caller WITH the Compliance role
{
  "companyNumber": "04458210",
  "companyName": "…",
  "stage": "…",
  "latestEvent": "…",
  "controllerRisk": "controller Ms Mary Patricia Kelleher runs no other distressed companies on record"
}
```

The analyst still gets the *actionable* distress signal (a controller risk exists,
the company's stage, the contracts at risk) — they simply do not get the **person's
identity**. That is textbook data-minimisation: least privilege on the PII, full
utility on everything else.

---

## 3. The config to enable it — `roles.conf` and policy wiring

Two things switch this on. **Neither is applied here; this is the proposed diff.**

### (a) Add the gating role (`roles.conf`)

The mounted `orbital-dev/config/roles.conf` already defines roles via
`grantedAuthorityMappings` and sets the baseline with `defaultUserRoleMappings` /
`defaultApiClientRoleMappings`. To gate PII you add **one new role** that the
policy keys off, and you make sure the *default* role does **not** carry it (so the
brain is private-by-default and PII access is the explicit exception).

Proposed additions (shown as the new blocks to merge into the existing file — do
not overwrite the current Admin / Viewer / QueryRunner / PlatformManager blocks):

```hocon
# New role whose presence the per-type policies check for.
# RunQuery lets the holder execute federated queries; the policy layer is what
# additionally un-masks PII for this role specifically.
grantedAuthorityMappings {
   Compliance {
      grantedAuthorities = [
         "BrowseSchema",
         "RunQuery",
         "ViewQueryHistory",      # so a compliance officer can audit their own access
         "ViewHistoricQueryResults"
      ]
   }
}

# Bind named principals to the Compliance role. Anyone NOT listed here keeps the
# default role (Viewer for users / QueryRunner for API clients) and therefore sees
# masked PII. Replace the example subjects with your IdP group / client ids.
userRoleMappings {
   "compliance-team@regulated-buyer.example" {
      roles = ["Compliance"]
   }
}
apiClientRoleMappings {
   "compliance-batch-client" {
      roles = ["Compliance"]
   }
}
```

The existing baseline already does the right thing for the "private-by-default"
posture, and is left **unchanged**:

```hocon
defaultUserRoleMappings    { roles = ["Viewer"] }       # users: schema-only, masked PII
defaultApiClientRoleMappings { roles = ["QueryRunner"] } # clients: can query, masked PII
```

So an ordinary analyst or API client can run the brain and get full company/markets/
crime intelligence, but **person-identifying fields come back masked** until they are
explicitly placed in the `Compliance` role.

### (b) The engine is already on

No change required — for visibility, the toggle already set in
`orbital-dev/docker-compose.yml` is:

```text
OPTIONS: >-
   …
   --vyne.toggles.policiesEnabled=true
```

If a future operator turned that off, the policies above would simply be ignored
(fail-open), so a regulated deployment should treat `policiesEnabled=true` as a
**required, monitored invariant** and assert it at startup.

### Rollout checklist (for whoever applies this later)

1. Create `orbital-dev/workspace/src/governance-policies.taxi` with the §2 snippet
   (new file — live models untouched).
2. Merge the §3(a) `Compliance` role + bindings into `orbital-dev/config/roles.conf`.
3. Confirm `--vyne.toggles.policiesEnabled=true` is present (§3b).
4. Re-run the §2 query as a default caller (expect masked `controllerRisk`) and as a
   `Compliance` caller (expect the real value) — the masking *is* the test.

---

## 4. The audit trail is already there — lineage as evidence

A regulated buyer's final requirement is *provability*: when a controller's name was
disclosed, show **who asked, what was returned, and which upstream produced it.**

Orbital's **lineage graph already provides this for every query**, with no extra
work. The same compose options that run the stack persist it:

```text
--vyne.analytics.persistResults=true
--vyne.analytics.persistRemoteCallResponses=true
```

For each executed query Orbital records the full **lineage**: the input facts, every
remote call it made (e.g. the `GET /insolvency?company_number=04458210` to the
insolvency service), the response payloads, and the typed field each value flowed
into — right down to `InsolvencyProfile.controllerRisk`. This gives a regulated buyer,
per disclosure, exactly the four things an auditor asks for:

- **What was disclosed** — the typed result, including whether `controllerRisk` was
  returned in the clear or as the redaction token.
- **To whom** — the caller identity / role attached to the query (from §3), with
  `ViewQueryHistory` letting compliance review their own access.
- **From where** — the precise upstream call and response that produced the personal
  data (provenance for a subject-access or regulator request).
- **Under which rule** — because masking is a *type-scoped policy*, the same lineage
  record shows the value passing through `ControllerRiskPolicy`, evidencing that the
  control was in force at disclosure time.

So the governance story for this brain is **fully expressible on the stack as it
runs today**: per-type policies (engine already enabled) supply the *control*,
`roles.conf` supplies the *who*, and the persisted lineage graph supplies the
*audit trail* — and this document changes none of them, it specifies the exact,
reviewable steps to turn them on.
