#!/usr/bin/env node
/**
 * AIRP RealProviderAdapter Node sidecar.
 *
 * Line-delimited JSON over stdio. Credentials come from process env only
 * (DEEPSEEK_API_KEY) — never from IPC request bodies.
 *
 * IPC contract (see docs/adr/0010-real-pi-sidecar-deepseek-e2e.md):
 *   Py→Node: {type:"stream", request_id, messages, tools, model, metadata?}
 *            {type:"abort",  request_id}
 *   Node→Py: {type:"delta",     request_id, text}
 *            {type:"tool_call", request_id, id, name, args}
 *            {type:"result",    request_id, usage, stop_reason, cost_estimate}
 *            {type:"error",     request_id, category, retryable, message}
 *            {type:"aborted",   request_id}
 *
 * Mock mode (no network/key): --mock arg or PI_SIDECAR_MOCK=1.
 */

import readline from "node:readline";

const MOCK =
  process.env.PI_SIDECAR_MOCK === "1" ||
  process.argv.includes("--mock");

const DEFAULT_MODEL = "deepseek-v4-flash";
const DEFAULT_PROVIDER = "deepseek";
const DEFAULT_BASE_URL = "https://api.deepseek.com";
const RATE_VERSION = "pi-catalog-0.82.1";

/** @type {Map<string, AbortController>} */
const active = new Map();

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function classifyError(err) {
  const msg = String(err?.message || err || "provider error");
  const lower = msg.toLowerCase();
  if (
    lower.includes("abort") ||
    err?.name === "AbortError" ||
    lower.includes("cancelled")
  ) {
    return { category: "provider_unavailable", retryable: false, aborted: true, message: msg };
  }
  if (
    lower.includes("429") ||
    lower.includes("rate limit") ||
    lower.includes("timeout") ||
    lower.includes("econnreset") ||
    lower.includes("econnrefused") ||
    lower.includes("fetch failed") ||
    lower.includes("503") ||
    lower.includes("502") ||
    lower.includes("overloaded") ||
    lower.includes("temporarily")
  ) {
    return { category: "provider_unavailable", retryable: true, aborted: false, message: msg };
  }
  if (
    lower.includes("401") ||
    lower.includes("403") ||
    lower.includes("invalid api") ||
    lower.includes("unauthorized") ||
    lower.includes("authentication") ||
    lower.includes("api key")
  ) {
    return { category: "provider_rejected", retryable: false, aborted: false, message: msg };
  }
  return { category: "terminal_internal", retryable: false, aborted: false, message: msg };
}

/**
 * Convert AIRP ProviderRequest messages into pi-ai Context messages.
 * AIRP messages are role-based dicts; tools are AIRP tool schemas.
 */
function toPiContext(messages, tools) {
  const piMessages = [];
  let systemPrompt = "";

  for (const msg of messages || []) {
    const role = msg?.role;
    if (role === "system") {
      const text =
        typeof msg.content === "string"
          ? msg.content
          : Array.isArray(msg.content)
            ? msg.content.map((c) => (typeof c === "string" ? c : c?.text || "")).join("")
            : JSON.stringify(msg.content ?? "");
      systemPrompt = systemPrompt ? `${systemPrompt}\n${text}` : text;
      continue;
    }
    if (role === "user") {
      const text =
        typeof msg.content === "string"
          ? msg.content
          : JSON.stringify(msg.content ?? "");
      piMessages.push({ role: "user", content: text, timestamp: Date.now() });
      continue;
    }
    if (role === "assistant") {
      const text =
        typeof msg.content === "string"
          ? msg.content
          : JSON.stringify(msg.content ?? "");
      // Replay the assistant turn including any tool_calls it issued, so a
      // following role:"toolResult" has a preceding toolCall to respond to.
      // pi-ai models tool calls as content-array ToolCall elements
      // (see pi-ai types.d.ts AssistantMessage.content / ToolCall).
      const content = [];
      if (text) content.push({ type: "text", text });
      const calls = Array.isArray(msg.tool_calls) ? msg.tool_calls : [];
      for (const call of calls) {
        content.push({
          type: "toolCall",
          id: call?.id || `call_${call?.name || "tool"}`,
          name: call?.name || "tool",
          arguments:
            call?.args && typeof call?.args === "object" ? call.args : {},
        });
      }
      piMessages.push({
        role: "assistant",
        content,
        api: "openai-completions",
        provider: DEFAULT_PROVIDER,
        model: DEFAULT_MODEL,
        usage: {
          input: 0,
          output: 0,
          cacheRead: 0,
          cacheWrite: 0,
          totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
        },
        stopReason: calls.length ? "toolUse" : "stop",
        timestamp: Date.now(),
      });
      continue;
    }
    if (role === "tool") {
      const name = msg.name || "tool";
      const content =
        typeof msg.content === "string"
          ? msg.content
          : JSON.stringify(msg.content ?? {});
      piMessages.push({
        role: "toolResult",
        // tool_call_id is set by the director to match the assistant tool_call id.
        toolCallId: msg.tool_call_id || msg.id || `tool_${name}`,
        toolName: name,
        content: [{ type: "text", text: content }],
        isError: false,
        timestamp: Date.now(),
      });
    }
  }

  const piTools = (tools || []).map((t) => airpToolToPi(t));
  const context = { messages: piMessages, tools: piTools };
  if (systemPrompt) context.systemPrompt = systemPrompt;
  return context;
}

function airpToolToPi(tool) {
  // AIRP tool schema is hand-rolled: {name, description, parameters:{required,optional,types}}
  // pi-ai expects TypeBox-like JSON Schema under `parameters`.
  const params = tool.parameters || {};
  const types = params.types || {};
  const required = params.required || [];
  const properties = {};
  for (const [key, t] of Object.entries(types)) {
    properties[key] = jsonSchemaForType(t, key);
  }
  return {
    name: tool.name,
    description: tool.description || "",
    parameters: {
      type: "object",
      properties,
      required,
      additionalProperties: true,
    },
  };
}

function jsonSchemaForType(t, key) {
  if (t === "str") return { type: "string", description: key };
  if (t === "int") return { type: "integer", description: key };
  if (t === "list") return { type: "array", items: {}, description: key };
  if (t === "dict") return { type: "object", additionalProperties: true, description: key };
  if (t === "bool") return { type: "boolean", description: key };
  return {};
}

function usageFromPi(usage) {
  const prompt = Number(usage?.input ?? 0);
  const completion = Number(usage?.output ?? 0);
  const total = Number(usage?.totalTokens ?? prompt + completion);
  return {
    prompt_tokens: prompt,
    completion_tokens: completion,
    total_tokens: total,
  };
}

function costFromPi(usage) {
  const amount = Number(usage?.cost?.total ?? 0);
  return {
    amount,
    currency: "USD",
    rate_version: RATE_VERSION,
  };
}

function mapStopReason(reason) {
  if (reason === "toolUse") return "tool_calls";
  if (reason === "length") return "length";
  if (reason === "stop") return "stop";
  if (reason === "aborted") return "aborted";
  if (reason === "error") return "error";
  return String(reason || "stop");
}

async function handleStreamMock(msg) {
  const requestId = msg.request_id;
  const controller = new AbortController();
  active.set(requestId, controller);

  try {
    if (controller.signal.aborted) {
      emit({ type: "aborted", request_id: requestId });
      return;
    }

    // Script selection via metadata.mock_script or default narrative+commit path.
    const script = msg.metadata?.mock_script || "text_then_commit";

    if (script === "error_retryable") {
      emit({
        type: "error",
        request_id: requestId,
        category: "provider_unavailable",
        retryable: true,
        message: "mock transient failure",
      });
      return;
    }
    if (script === "error_terminal") {
      emit({
        type: "error",
        request_id: requestId,
        category: "provider_rejected",
        retryable: false,
        message: "mock terminal rejection",
      });
      return;
    }
    if (script === "hang_until_abort") {
      await new Promise((resolve) => {
        if (controller.signal.aborted) {
          resolve();
          return;
        }
        controller.signal.addEventListener("abort", () => resolve(), { once: true });
      });
      emit({ type: "aborted", request_id: requestId });
      return;
    }
    if (script === "crash") {
      process.stderr.write("mock sidecar intentional crash\n");
      process.exit(97);
    }

    // Default: stream Chinese text, then optional tool_call from tools list.
    const chunks = ["海风", "掠过礁石。", "浪花轻拍岸边。"];
    for (const chunk of chunks) {
      if (controller.signal.aborted) {
        emit({ type: "aborted", request_id: requestId });
        return;
      }
      emit({ type: "delta", request_id: requestId, text: chunk });
      await new Promise((r) => setTimeout(r, 5));
    }

    if (controller.signal.aborted) {
      emit({ type: "aborted", request_id: requestId });
      return;
    }

    const tools = msg.tools || [];
    const commitTool = tools.find((t) => t.name === "commit_turn_draft");
    if (commitTool || script === "tool_call") {
      const draft = msg.metadata?.mock_draft || {
        polished_input: "我走向礁石",
        content: "<p>海风掠过礁石。浪花轻拍岸边。</p>",
        summary: "玩家来到海边",
        options: '<font color="#5a7a5a">继续观察海面</font>',
        mvu_commands: "_.set('世界.时间', '1月1日 10:00');",
      };
      const expected = msg.metadata?.mock_expected_revision ?? 0;
      emit({
        type: "tool_call",
        request_id: requestId,
        id: "mock_call_1",
        name: "commit_turn_draft",
        args: { draft, expected_revision: expected },
      });
      emit({
        type: "result",
        request_id: requestId,
        usage: { prompt_tokens: 120, completion_tokens: 80, total_tokens: 200 },
        stop_reason: "tool_calls",
        cost_estimate: { amount: 0.001, currency: "USD", rate_version: "mock-rates-v0" },
      });
      return;
    }

    emit({
      type: "result",
      request_id: requestId,
      usage: { prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 },
      stop_reason: "stop",
      cost_estimate: { amount: 0.0005, currency: "USD", rate_version: "mock-rates-v0" },
    });
  } finally {
    active.delete(requestId);
  }
}

async function handleStreamReal(msg) {
  const requestId = msg.request_id;
  const controller = new AbortController();
  active.set(requestId, controller);

  try {
    const { createModels } = await import("@earendil-works/pi-ai");
    const { deepseekProvider } = await import(
      "@earendil-works/pi-ai/providers/deepseek"
    );

    const models = createModels();
    models.setProvider(deepseekProvider());

    const modelId = msg.model || DEFAULT_MODEL;
    let model = models.getModel(DEFAULT_PROVIDER, modelId);
    if (!model) {
      // Allow explicit base_url override via metadata without leaking secrets.
      const baseUrl = msg.metadata?.base_url || DEFAULT_BASE_URL;
      emit({
        type: "error",
        request_id: requestId,
        category: "provider_rejected",
        retryable: false,
        message: `unknown model ${modelId} for provider ${DEFAULT_PROVIDER} (base_url=${baseUrl})`,
      });
      return;
    }

    // Optional base_url override for proxies — never log credentials.
    if (msg.metadata?.base_url && msg.metadata.base_url !== model.baseUrl) {
      model = { ...model, baseUrl: msg.metadata.base_url };
    }

    if (!process.env.DEEPSEEK_API_KEY) {
      emit({
        type: "error",
        request_id: requestId,
        category: "provider_rejected",
        retryable: false,
        message: "DEEPSEEK_API_KEY is not set in sidecar process env",
      });
      return;
    }

    const context = toPiContext(msg.messages, msg.tools);
    const stream = models.stream(model, context, {
      signal: controller.signal,
      // Keep thinking off for the narrative flash path unless requested.
      // stream() accepts provider options; reasoning is on streamSimple.
    });

    let sawDone = false;
    let finalMessage = null;

    for await (const event of stream) {
      if (controller.signal.aborted) {
        emit({ type: "aborted", request_id: requestId });
        return;
      }
      switch (event.type) {
        case "text_delta":
          if (event.delta) {
            emit({ type: "delta", request_id: requestId, text: event.delta });
          }
          break;
        case "toolcall_end": {
          const call = event.toolCall;
          emit({
            type: "tool_call",
            request_id: requestId,
            id: call.id,
            name: call.name,
            args: call.arguments || {},
          });
          break;
        }
        case "done":
          sawDone = true;
          finalMessage = event.message;
          break;
        case "error": {
          if (event.reason === "aborted" || controller.signal.aborted) {
            emit({ type: "aborted", request_id: requestId });
            return;
          }
          const classified = classifyError(
            event.error?.errorMessage || event.error || "stream error"
          );
          emit({
            type: "error",
            request_id: requestId,
            category: classified.category,
            retryable: classified.retryable,
            message: classified.message,
          });
          return;
        }
        default:
          break;
      }
    }

    if (controller.signal.aborted) {
      emit({ type: "aborted", request_id: requestId });
      return;
    }

    if (!finalMessage) {
      try {
        finalMessage = await stream.result();
      } catch (err) {
        if (controller.signal.aborted) {
          emit({ type: "aborted", request_id: requestId });
          return;
        }
        const classified = classifyError(err);
        if (classified.aborted) {
          emit({ type: "aborted", request_id: requestId });
          return;
        }
        emit({
          type: "error",
          request_id: requestId,
          category: classified.category,
          retryable: classified.retryable,
          message: classified.message,
        });
        return;
      }
    }

    if (finalMessage?.stopReason === "aborted" || controller.signal.aborted) {
      emit({ type: "aborted", request_id: requestId });
      return;
    }
    if (finalMessage?.stopReason === "error") {
      const classified = classifyError(finalMessage.errorMessage || "provider error");
      emit({
        type: "error",
        request_id: requestId,
        category: classified.category,
        retryable: classified.retryable,
        message: classified.message,
      });
      return;
    }

    // Emit any tool calls that only appeared in the final message (non-streamed).
    if (!sawDone && finalMessage?.content) {
      for (const block of finalMessage.content) {
        if (block.type === "toolCall") {
          emit({
            type: "tool_call",
            request_id: requestId,
            id: block.id,
            name: block.name,
            args: block.arguments || {},
          });
        }
      }
    }

    emit({
      type: "result",
      request_id: requestId,
      usage: usageFromPi(finalMessage?.usage),
      stop_reason: mapStopReason(finalMessage?.stopReason),
      cost_estimate: costFromPi(finalMessage?.usage),
    });
  } catch (err) {
    if (controller.signal.aborted) {
      emit({ type: "aborted", request_id: requestId });
      return;
    }
    const classified = classifyError(err);
    if (classified.aborted) {
      emit({ type: "aborted", request_id: requestId });
      return;
    }
    emit({
      type: "error",
      request_id: requestId,
      category: classified.category,
      retryable: classified.retryable,
      message: classified.message,
    });
  } finally {
    active.delete(requestId);
  }
}

function handleAbort(msg) {
  const requestId = msg.request_id;
  const controller = active.get(requestId);
  if (controller) {
    controller.abort();
  } else {
    // No active stream — still acknowledge so Python does not hang.
    emit({ type: "aborted", request_id: requestId });
  }
}

async function handleMessage(msg) {
  if (!msg || typeof msg !== "object") return;
  const type = msg.type;
  if (type === "stream") {
    if (MOCK) {
      await handleStreamMock(msg);
    } else {
      await handleStreamReal(msg);
    }
    return;
  }
  if (type === "abort") {
    handleAbort(msg);
    return;
  }
  if (msg.request_id) {
    emit({
      type: "error",
      request_id: msg.request_id,
      category: "terminal_internal",
      retryable: false,
      message: `unknown ipc type: ${type}`,
    });
  }
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

rl.on("line", (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  let msg;
  try {
    msg = JSON.parse(trimmed);
  } catch (err) {
    process.stderr.write(`sidecar json parse error: ${err}\n`);
    return;
  }
  // Serialize per-line handling so mock timings stay ordered; real streams
  // still multiplex via AbortControllers keyed by request_id.
  handleMessage(msg).catch((err) => {
    const requestId = msg?.request_id;
    if (requestId) {
      const classified = classifyError(err);
      emit({
        type: "error",
        request_id: requestId,
        category: classified.category,
        retryable: classified.retryable,
        message: classified.message,
      });
    } else {
      process.stderr.write(`sidecar unhandled: ${err}\n`);
    }
  });
});

rl.on("close", () => {
  for (const controller of active.values()) {
    controller.abort();
  }
  active.clear();
  process.exit(0);
});
