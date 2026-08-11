# Spec: Agent-bound JavaScript Regex Collections

## Status

Implemented (initial vertical slice). The Agent-owned collection schema, Studio CRUD/test panel, JavaScript runtime transformer, plan snapshotting, trace hooks, independent active-Graph selection, content-neutral host commit, and Project editor cleanup are implemented. Remaining compatibility behavior is explicitly called out below.

This specification supersedes the Project-selected RP Turn Adapter configuration described in the current Agent Studio documents. `TurnAdapter` and `RPTurnAdapter` are removed from the normal runtime. Historic tagged text is parsed only by the one-time compatibility importer at `airp.compat.legacy_turn_import`; it is not a runtime adapter or a Project configuration.

## Problem

AIRP currently exposes an RP Turn Adapter on each Project. That object mixes several unrelated responsibilities:

- opening-prompt assembly;
- final-node role requirements;
- tagged RP output parsing;
- regular-expression configuration;
- conversion from a Graph output Artifact into the story commit format.

The result is attached to the wrong object. Text cleanup and extraction vary by Agent and by that Agent's prompt contract, not by Project. A planner may emit tagged notes, a reviewer may emit JSON-like diagnostics, and the final writer may wrap prose in `<content>` tags. A Project-level parser cannot express those differences cleanly.

AIRP needs a reusable, visible and fully debuggable text-transformation mechanism. The engine must not invent tags, writing formats or extraction rules. Users must be able to define all such behavior in Studio and bind it to the Agent that produces or consumes the text.

## Goals

1. Add reusable `Regex Collection` objects to the global Studio Library.
2. Let each Agent Definition bind zero or one Regex Collection.
3. Apply enabled rules in list order to an Agent's input, output or both.
4. Use JavaScript `RegExp` matching and JavaScript replacement-string semantics.
5. Preserve raw and transformed values in Node Run diagnostics.
6. Make invalid patterns and failed transformations visible as node failures.
7. Remove Project-level Turn Adapter controls and hidden content-format assumptions.
8. Keep Graph Runtime independent from Regex Collection persistence and from RP story semantics.
9. Keep the active Graph choice independent from Project content and persist it as runtime selection state.

## Non-goals

- Defining built-in `<content>`, `<summary>`, `<options>` or MVU tag rules.
- Automatically inferring a replacement from a pattern.
- Supporting arbitrary JavaScript functions in rules.
- Allowing Graph Nodes or Projects to override an Agent's Regex Collection.
- Applying transformations to Provider credentials, tool payloads or structured configuration.
- Maintaining Regex Collection versions or per-run editable copies.
- Supporting multiple collections on one Agent in the first implementation.
- Persisting `graph_id` or `turn_adapter` in a Project definition.

## Domain Model

### Regex Collection

A Regex Collection is a reusable global Studio Library object:

```json
{
  "id": "extract-final-prose",
  "name": "Extract final prose",
  "rules": [
    {
      "id": "unwrap-content",
      "name": "Unwrap content tag",
      "enabled": true,
      "target": "output",
      "pattern": "^[\\s\\S]*?<content>([\\s\\S]*?)</content>[\\s\\S]*$",
      "flags": "",
      "replacement": "$1"
    }
  ],
  "created_at": 0,
  "updated_at": 0
}
```

The fields have these meanings:

| Field | Meaning |
|---|---|
| `id` | Stable library identifier. Generated when omitted and immutable after creation. |
| `name` | User-facing name. Duplicate names receive `-copy`, `-copy-2`, and so on. |
| `rules` | Ordered list. Array order is execution order. |
| `rule.id` | Stable identifier within the collection. |
| `rule.name` | User-facing diagnostic label. |
| `rule.enabled` | Disabled rules are retained but skipped. |
| `rule.target` | One of `input`, `output`, or `both`. |
| `rule.pattern` | JavaScript `RegExp` source without surrounding `/` delimiters. |
| `rule.flags` | JavaScript flags such as `g`, `i`, `m`, `s`, `u`, or `y`. |
| `rule.replacement` | JavaScript replacement text, including `$&`, `$1` through `$99`, ``$` ``, and `$'`. |

The Studio displays the assembled expression, for example `/pattern/g`, but stores `pattern` and `flags` separately. This avoids ambiguous parsing when the pattern itself contains `/`.

A rule is a replacement operation, not merely a matcher. Extraction is expressed with a capture group and replacement such as `$1`. AIRP does not guess which capture group represents the desired output.

### Agent Definition Binding

Agent Definition gains one nullable field:

```json
{
  "agent_id": "writer",
  "regex_collection_id": "extract-final-prose"
}
```

`null` means pass input and output through unchanged. The collection is resolved and frozen into the Execution Plan at Graph Run start, exactly like the Agent's instruction and model configuration. Editing a collection affects future runs but never mutates an in-progress or historical run.

Deletion of a Regex Collection is blocked while any Agent Definition references it. The error lists referencing Agent IDs and names. Copying a collection produces a new stable ID and an automatically suffixed name; Agents remain bound to the original.

### Project And Active Graph Selection

A Project owns normalized role-play content, openings, variables, assets and
Worldbook bindings. It does not own execution selection state and therefore
does not persist `graph_id` or `turn_adapter`. The active Graph is selected by
the game Runtime Config and persisted independently, one selection per active
Project, through the runtime's active-Graph selection store. Changing that
selection affects the next run without editing Project content.

## Runtime Semantics

### Module Seam

Text transformation belongs behind one deep module with a small interface:

```text
transform(text, frozen_rules, target) -> transformed text + rule diagnostics
```

The module owns JavaScript execution, ordered rule application, replacement behavior and error normalization. Graph Runtime only schedules nodes and passes Artifacts. Provider Node Runner invokes the module for the current resolved Agent. Persistence and HTTP code never perform transformations.

The production implementation must use an actual JavaScript `RegExp` runtime. It must not emulate JavaScript expressions with Python `re`, because syntax and replacement behavior differ. AIRP does not use a provider sidecar; Node.js is only an implementation dependency of the JavaScript Regex worker. The worker may be long-lived behind the Regex Transformer seam, and must not spawn a new Node process for every rule or token delta.

### Input Order

For each node:

1. Receive the upstream `AgentArtifact`.
2. Resolve the Agent's frozen Regex Collection from the Execution Plan.
3. Apply all enabled `input` and `both` rules in collection order.
4. Build runtime macro context from the transformed input.
5. Expand instruction and prompt macros, including `{{handoff}}` and `{{node_input}}`.
6. Append the transformed input as the Provider user message.
7. Call the Provider.

This guarantees that the Agent's prompt preview and actual request agree about the text the Agent receives.

### Output Order

For each successful model call:

1. Stream raw Provider deltas to the live Node Run trace.
2. Assemble the complete raw model output.
3. Apply all enabled `output` and `both` rules in collection order.
4. Store both raw output and transformed output in Node Run detail.
5. Emit the transformed output as the node's `AgentArtifact`.
6. Pass that Artifact to the next node, or to the host `graph_turn_commit` seam when it is the Graph output node.

Output regex rules are not applied independently to streaming chunks. Chunk boundaries are arbitrary and may split tags or capture groups. The live trace labels streaming text as raw; when generation finishes, it shows the final transformed output and per-rule results.

Tool-call rounds follow the same rule: output transformation is applied only to the terminal textual response that becomes the node Artifact. Intermediate assistant text associated with tool calls remains raw diagnostic material and is not handed to downstream nodes.

### Failure Behavior

The following conditions fail the current node and therefore fail the entire Graph Run:

- invalid JavaScript pattern or flags;
- JavaScript worker unavailable or protocol failure;
- transformation result is not a string;
- configured execution limit is exceeded.

There is no automatic retry, rule skipping or fallback to raw text. The node error identifies the Regex Collection, rule ID, rule name, target and JavaScript error message. The user edits the collection and explicitly reruns the whole Graph.

An expression that matches nothing is not an error; it leaves the text unchanged and records `matched: false` in diagnostics.

To prevent accidental denial of service from pathological expressions, the JavaScript worker must enforce a per-transform timeout and maximum input/output byte size. These are Runtime safety limits, not user-editable writing rules.

## Trace And Debugging

Node Run detail adds:

```json
{
  "regex_collection": {
    "id": "extract-final-prose",
    "name": "Extract final prose"
  },
  "input_transform": {
    "raw": "...",
    "transformed": "...",
    "rules": []
  },
  "output_transform": {
    "raw": "<content>...</content>",
    "transformed": "...",
    "rules": [
      {
        "rule_id": "unwrap-content",
        "name": "Unwrap content tag",
        "matched": true,
        "changed": true
      }
    ]
  }
}
```

Diagnostics store complete raw and transformed text because the Studio is explicitly a prompt and model-output debugging tool. Existing secret redaction still applies to configuration, but AIRP does not truncate Agent input/output in the current and most recent run records.

Debug Replay uses the original frozen raw node input and the currently saved Agent Definition plus its currently bound Regex Collection. The comparison view shows old raw/transformed values beside replay raw/transformed values. Replay does not mutate the story.

## Studio UX

Studio gains a sixth primary view: `Regex Collections`.

The view contains:

- collection list;
- new, save, copy and delete actions;
- collection name;
- ordered rule editors;
- add, remove, move up and move down controls;
- rule name;
- enabled toggle;
- target selector with Input, Output and Both;
- pattern, flags and replacement fields;
- a test area with input text and visible transformed output;
- per-rule matched/changed/error results from the same JavaScript engine used by Runtime.

The Agent editor gains a `Regex Collection` selector with `None` as the default. Its prompt preview shows the transformed preview input and the exact frozen rules that would apply.

The Regex Collections view does not duplicate this binding selector. It owns Collection CRUD and test diagnostics; the Agent editor is the sole UI owner of `regex_collection_id`.

The Project editor removes:

- `Turn Adapter`;
- `Turn Adapter config`;
- `Selected Graph`;
- the redundant `Load project` button.

Clicking a Project list row remains the loading mechanism. The game Runtime Config graph selector remains the only active-Graph selector. Project saves contain neither `graph_id` nor `turn_adapter`; active-Graph selection is stored independently by the runtime.

## Content-neutral Story Commit

New Studio runs do not require tagged output. The Graph output node's
transformed `AgentArtifact` is handed to the host
`airp.host.graph_turn_commit` entry point and submitted as-is. The host wraps
the opaque text in its minimal turn commit shape; it does not infer tags,
sections, prose format, summary, options, MVU commands or writing policy.

The Agent Framework remains unaware of story fields. It returns only the
transformed output Artifact. The host runtime remains responsible for atomic
commit, session revision and projection.

`TurnAdapter` and `RPTurnAdapter` are not part of the runtime architecture and
are not user-selectable. Historic `<content>`, `<summary>`, `<options>` and
`<UpdateVariable>` text is handled only by the one-time
`airp.compat.legacy_turn_import` path while importing old records. New runs,
Graph replay and Regex Collection execution never invoke that parser. No
built-in tag pattern is copied into a new Agent or collection, and no Project
save writes `turn_adapter` or `graph_id`.

## HTTP Contract

The maintained Studio endpoints are:

```text
GET    /v1/studio/regex-collections
POST   /v1/studio/regex-collections
GET    /v1/studio/regex-collections/{id}
PUT    /v1/studio/regex-collections/{id}
POST   /v1/studio/regex-collections/{id}/copy
POST   /v1/studio/regex-collections/{id}/test
DELETE /v1/studio/regex-collections/{id}
```

The test endpoint accepts a target and sample text, executes the currently supplied unsaved collection payload when present, and returns transformed text plus rule diagnostics. This lets users debug edits before saving.

Agent CRUD accepts and returns `regex_collection_id`. Execution Plan and Node Run payloads embed a frozen, secret-free Regex Collection snapshot so historical traces do not depend on mutable library files.

## Acceptance Criteria

1. A user can create, edit, reorder, copy, test and delete an unreferenced Regex Collection in Studio.
2. A user can bind one collection to an Agent and clear the binding.
3. Deleting a collection referenced by an Agent returns `409` and lists all references.
4. Input rules transform the exact text used by macros and the Provider user message.
5. Output rules transform the Artifact handed to the next node and the Graph output Artifact.
6. Rules execute in visible list order and `both` runs at both stages.
7. Runtime uses JavaScript regex and replacement semantics, including `$1` capture replacement.
8. Raw streamed output remains visible while final transformed output appears on completion.
9. Node detail includes raw input/output, transformed input/output and per-rule diagnostics.
10. Invalid regex fails the node, identifies the rule and stops the Graph without hidden retry.
11. A no-match rule succeeds unchanged and is reported as not matched.
12. A collection edit affects the next run but not an already compiled or historical run.
13. New Projects contain no user-facing Turn Adapter configuration.
14. Studio Project has no Graph selector or redundant Load button.
15. No built-in writing tags, extraction patterns or prose-format constraints are created by AIRP.

## Test Strategy

Tests target public module and transport seams:

- JavaScript worker contract tests for flags, global replacement, capture groups, multiline text, no-match, order, invalid syntax, timeout and process recovery;
- Regex Collection library tests for CRUD, stable IDs, copy suffixes, normalization and Agent-reference deletion protection;
- Agent Definition tests for binding validation, clearing, copy behavior and frozen Execution Plan snapshots;
- Node Runner tests for input-before-macro ordering, output-after-stream ordering, downstream transformed Artifact handoff and terminal tool-call handling;
- Graph Runtime tests proving rule errors fail one node and terminate the complete run;
- HTTP tests for collection CRUD/test endpoints and error payloads;
- Node Run persistence tests for raw/transformed values and rule diagnostics;
- Debug Replay tests proving current Agent/collection configuration is used without story mutation;
- browser tests covering collection editing, Agent binding, live raw output, final transformed output and node detail;
- migration tests proving old card/project inputs can be imported while new Project saves contain neither `turn_adapter` nor `graph_id`; legacy tagged text is parsed only by `legacy_turn_import`.

Tests must not assert private file layout or JavaScript-worker implementation details. The transformation module interface and Runtime Studio HTTP contract are the primary test surfaces.

## Implementation Plan

### Phase 1: Regex transformation module

1. Define normalized collection/rule types and user-facing errors.
2. Implement the long-lived JavaScript execution worker and Python process adapter.
3. Add ordered input/output transformation and structured diagnostics.
4. Cover JavaScript behavior, limits, failure and worker recovery through the module interface.

### Phase 2: Library and Agent ownership

1. Add Workspace `regex_collections_root`.
2. Add file-backed Regex Collection library and Application assembly.
3. Add CRUD, copy, test and dependency-aware delete operations.
4. Add nullable `regex_collection_id` to Agent Definitions.
5. Resolve and freeze the selected collection during Execution Plan compilation.

### Phase 3: Runtime and trace integration

1. Inject the transformer into Provider Node Runner.
2. Transform input before macro/prompt construction.
3. Preserve raw streaming deltas and transform only the completed terminal output.
4. Hand transformed Artifacts between nodes.
5. Persist raw/transformed values and per-rule diagnostics in Node Run detail.
6. Apply current bindings during Debug Replay.

### Phase 4: Studio

1. Add the Regex Collections navigation view and full editor.
2. Add test input/output and per-rule diagnostics.
3. Add the Agent binding selector and preview integration.
4. Remove Project Turn Adapter controls.
5. Remove Project Selected Graph and Load Project controls.
6. Keep game Runtime Config as the sole active-Graph selector.

### Phase 5: Content-neutral commit and compatibility

1. Keep `graph_turn_commit` as the content-neutral host commit boundary for new Graph output.
2. Remove runtime selection and execution of `TurnAdapter`/`RPTurnAdapter`.
3. Retain only the explicit one-time `legacy_turn_import` path for older tagged records.
4. Ensure newly saved Projects contain neither `turn_adapter` nor `graph_id`; persist active Graph selection independently.
5. Document and test that no legacy tag-pattern migration is inferred into Regex Collections.

### Phase 6: Verification

1. Run focused module, Agent, Graph, Runtime, HTTP and browser suites.
2. Run the full test suite and package build.
3. Perform a real-provider multi-node run with one input and one output rule.
4. Verify Studio and game views at desktop and mobile widths.
5. Update architecture, feature-status and handoff documents only after behavior is implemented and verified.

## Recommended Delivery Slices

The implementation should be delivered as tracer bullets with explicit blocking relationships:

```text
R1 JavaScript transform module
 └─> R2 Regex Collection library + HTTP CRUD
      ├─> R3 Agent binding + frozen plan
      │    └─> R4 Node Runner + trace + replay
      └─> R5 Studio collection editor
           └─> R6 Agent selector + Project cleanup
R4 + R6 ─> R7 content-neutral commit migration
R7       ─> R8 real-provider/browser acceptance
```

Each slice must end with its focused tests passing. No slice may introduce a hidden default Regex Collection, built-in tag rule or automatic recovery behavior.
