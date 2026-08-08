# Pi/Handoff Review Fix Verification

Date: 2026-08-09

## Runtime Findings

- Handoff loop expansion previously generated `<source>.loop<N>` identities without checking source node identities. `ExecutionPlanCompiler` now rejects a collision before a run starts.
- `PiCoreNodeRunner.close_execution()` previously skipped cleanup when the close write raised. Cleanup now always terminates or kills, waits, closes stdin/stdout/stderr, and joins reader threads.
- Sidecar stderr was discarded. The runner now retains only the final 16 KiB, redacts the active API key, and reports process exits as `pi_sidecar_failed` so Graph/Trace consumers receive a stable structured error.
- Pi previously called private `ProviderNodeRunner` helpers. Content serialization, observer notification, tool schemas, and resolved parameters now live in the content-neutral `airp.engine.runner_support` module.
- A changed session fingerprint previously reset the Pi Agent and erased its private transcript. The sidecar now constructs an Agent with the new system prompt, tools, model, and generation controls while carrying forward the previous message snapshot for the same execution and Agent.

## Packaging Evidence

Pi Agent Core 0.83 requires Node 22.19 or newer. Verification used Node 22.22.3.

The first esbuild ESM bundle was not usable: startup failed in the bundled `yaml` dependency with `Dynamic require of "process" is not supported`. A `createRequire(import.meta.url)` banner resolves that CommonJS compatibility path. The build is reproducible with:

```text
npm run build:pi-sidecar
```

The generated `pi_agent_sidecar.bundle.mjs` is the runtime default and remains package data under the existing `airp = ["resources/**/*"]` rule. It was launched from `/tmp`, outside the repository dependency tree, and returned the expected JSONL `protocol_error` for malformed input.

A local wheel build succeeded:

```text
airp-0.1.0-py3-none-any.whl
airp/resources/pi_agent_sidecar.bundle.mjs  1,120,315 bytes
```

The wheel therefore does not require npm packages at runtime. It still requires a compatible `node` executable on PATH; Node itself is not embedded in the Python wheel.

## Regression Coverage

- compile-time generated/source node identity collision
- close-write failure with process wait and pipe closure
- bounded, redacted stderr in the stable graph error
- transcript retention across model/generation overrides
- transcript retention while applying new system prompt and tool allowlist
