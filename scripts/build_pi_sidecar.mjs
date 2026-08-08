import { build } from "esbuild";

await build({
  entryPoints: ["src/airp/resources/pi_agent_sidecar.mjs"],
  bundle: true,
  platform: "node",
  format: "esm",
  target: "node22",
  banner: { js: 'import { createRequire } from "node:module"; const require = createRequire(import.meta.url);' },
  outfile: "src/airp/resources/pi_agent_sidecar.bundle.mjs",
  logLevel: "info",
});
