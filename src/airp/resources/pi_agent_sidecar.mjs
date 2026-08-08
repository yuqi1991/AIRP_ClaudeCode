#!/usr/bin/env node
/*
 * JSONL bridge for AIRP's ephemeral Pi Agent sessions.
 *
 * This process owns no durable data. Python starts a run, services only the
 * declared AIRP tools, and closes every execution id when GraphRuntime exits.
 */
import readline from "node:readline";
import { Agent } from "@earendil-works/pi-agent-core";
import { Type } from "typebox";
import { streamSimple as streamChatCompletions } from "@earendil-works/pi-ai/api/openai-completions";
import { streamSimple as streamResponses } from "@earendil-works/pi-ai/api/openai-responses";

const sessions = new Map();
const pendingTools = new Map();
let activeRun = null;

function emit(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function textContent(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((block) => block && block.type === "text")
    .map((block) => String(block.text || ""))
    .join("");
}

function traceMessages(messages) {
  return messages.map((message) => {
    if (message.role === "toolResult") {
      return {
        role: "tool",
        tool_call_id: message.toolCallId,
        name: message.toolName,
        content: textContent(message.content),
      };
    }
    if (message.role === "assistant") {
      return {
        role: "assistant",
        content: textContent(message.content),
        tool_calls: (message.content || [])
          .filter((block) => block && block.type === "toolCall")
          .map((block) => ({ id: block.id, name: block.name, args: block.arguments || {} })),
      };
    }
    return { role: message.role || "user", content: textContent(message.content) };
  });
}

function usagePayload(usage) {
  return {
    prompt_tokens: Number(usage?.input || 0),
    completion_tokens: Number(usage?.output || 0),
    total_tokens: Number(usage?.totalTokens || 0),
  };
}

function modelFor(config) {
  const modelId = String(config.model_id || "");
  return {
    id: modelId,
    name: modelId,
    api: config.api_format === "responses" ? "openai-responses" : "openai-completions",
    provider: "airp",
    baseUrl: String(config.base_url || ""),
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 128000,
    maxTokens: 16384,
  };
}

function generationOptions(generation) {
  const options = {};
  if (typeof generation?.temperature === "number") options.temperature = generation.temperature;
  const maxTokens = generation?.max_output_tokens ?? generation?.max_tokens;
  if (typeof maxTokens === "number" && maxTokens > 0) options.maxTokens = maxTokens;
  const reasoning = generation?.reasoning_effort ?? generation?.reasoning;
  if (typeof reasoning === "string" && reasoning !== "off") options.reasoning = reasoning;
  return options;
}

function providerPayloadOptions(generation) {
  const protectedKeys = new Set([
    "model", "messages", "input", "prompt", "tools", "stream", "stream_options",
    "api_key", "apikey", "secret", "authorization", "auth", "credentials", "headers",
    "base_url", "metadata", "temperature", "max_output_tokens", "max_tokens",
    "reasoning_effort", "reasoning",
  ]);
  const options = {};
  for (const [key, value] of Object.entries(generation || {})) {
    if (!protectedKeys.has(String(key).toLowerCase())) options[key] = value;
  }
  return options;
}

function toolDefinition(raw, session) {
  const functionSpec = raw?.function && typeof raw.function === "object" ? raw.function : raw || {};
  const name = String(functionSpec.name || "");
  return {
    name,
    label: name,
    description: String(functionSpec.description || ""),
    parameters: Type.Unsafe(functionSpec.parameters || { type: "object", properties: {} }),
    executionMode: "sequential",
    async execute(toolCallId, params) {
      const requestId = `${session.activeRunId}:${toolCallId}`;
      const result = await new Promise((resolve) => {
        pendingTools.set(requestId, resolve);
        emit({
          type: "tool_call",
          run_id: session.activeRunId,
          request_id: requestId,
          tool_call: { id: toolCallId, name, args: params || {} },
        });
      });
      if (result?.error) {
        return {
          content: [{ type: "text", text: String(result.error) }],
          details: { error: result.error },
          isError: true,
        };
      }
      const value = result?.value ?? {};
      return {
        content: [{ type: "text", text: typeof value === "string" ? value : JSON.stringify(value) }],
        details: value,
      };
    },
  };
}

function sessionKey(command) {
  return `${command.execution_id}:${command.agent_id}`;
}

function fingerprint(command) {
  const model = command.model || {};
  return JSON.stringify({
    system_prompt: command.system_prompt || "",
    seed_messages: command.seed_messages || [],
    tools: command.tools || [],
    model: {
      api_format: model.api_format,
      base_url: model.base_url,
      model_id: model.model_id,
    },
    generation: command.generation || {},
  });
}

function createSession(command, retainedMessages = null) {
  const config = command.model || {};
  const session = {
    key: sessionKey(command),
    fingerprint: fingerprint(command),
    activeRunId: null,
    lastAssistant: null,
    generation: command.generation || {},
    config,
    agent: null,
  };
  const stream = config.api_format === "responses" ? streamResponses : streamChatCompletions;
  const options = generationOptions(session.generation);
  const payloadOptions = providerPayloadOptions(session.generation);
  session.agent = new Agent({
    initialState: {
      systemPrompt: String(command.system_prompt || ""),
      model: modelFor(config),
      messages: Array.isArray(retainedMessages)
        ? retainedMessages
        : (Array.isArray(command.seed_messages) ? command.seed_messages : []),
      tools: (command.tools || []).map((tool) => toolDefinition(tool, session)),
    },
    getApiKey: () => String(config.api_key || ""),
    streamFn: (model, context, baseOptions) => stream(model, context, { ...baseOptions, ...options }),
    onPayload: (payload) => (
      payload && typeof payload === "object" ? { ...payload, ...payloadOptions } : payload
    ),
    toolExecution: "sequential",
    sessionId: command.execution_id,
  });
  session.agent.subscribe((event) => {
    const runId = session.activeRunId;
    if (!runId) return;
    if (event.type === "message_start" && event.message?.role === "assistant") {
      emit({
        type: "model_call_started",
        run_id: runId,
        system_prompt: String(command.system_prompt || ""),
        messages: traceMessages(session.agent.state.messages),
        model: config.model_id,
      });
      return;
    }
    if (event.type === "message_update" && event.assistantMessageEvent?.type === "text_delta") {
      emit({ type: "delta", run_id: runId, text: event.assistantMessageEvent.delta || "" });
      return;
    }
    if (event.type === "message_end" && event.message?.role === "assistant") {
      session.lastAssistant = event.message;
      const text = textContent(event.message.content);
      const payload = {
        run_id: runId,
        text,
        usage: usagePayload(event.message.usage),
        stop_reason: event.message.stopReason || "stop",
      };
      emit({
        type: event.message.stopReason === "error" || event.message.stopReason === "aborted"
          ? "model_call_failed"
          : "model_call_finished",
        ...payload,
        error: event.message.errorMessage || null,
      });
    }
  });
  return session;
}

async function run(command) {
  if (activeRun) throw new Error("sidecar already has an active run");
  const key = sessionKey(command);
  const nextFingerprint = fingerprint(command);
  let session = sessions.get(key);
  if (!session || session.fingerprint !== nextFingerprint) {
    const retainedMessages = session ? session.agent.state.messages.slice() : null;
    session = createSession(command, retainedMessages);
    sessions.set(key, session);
  }
  activeRun = command.run_id;
  session.activeRunId = command.run_id;
  session.lastAssistant = null;
  try {
    await session.agent.prompt(String(command.input || ""));
    const last = session.lastAssistant;
    if (!last || last.stopReason === "error" || last.stopReason === "aborted") {
      emit({
        type: "run_finished",
        run_id: command.run_id,
        ok: false,
        error: last?.errorMessage || (last?.stopReason === "aborted" ? "aborted" : "Pi Agent produced no final output"),
        aborted: last?.stopReason === "aborted",
      });
      return;
    }
    emit({
      type: "run_finished",
      run_id: command.run_id,
      ok: true,
      text: textContent(last.content),
      usage: usagePayload(last.usage),
      stop_reason: last.stopReason || "stop",
    });
  } finally {
    session.activeRunId = null;
    activeRun = null;
  }
}

async function handle(command) {
  if (!command || typeof command !== "object") return;
  if (command.type === "tool_result") {
    const resolve = pendingTools.get(command.request_id);
    if (resolve) {
      pendingTools.delete(command.request_id);
      resolve(command);
    }
    return;
  }
  if (command.type === "abort") {
    const prefix = `${command.execution_id}:`;
    for (const [key, session] of sessions) {
      if (key.startsWith(prefix)) session.agent.abort();
    }
    return;
  }
  if (command.type === "close") {
    const prefix = `${command.execution_id}:`;
    for (const [key, session] of sessions) {
      if (key.startsWith(prefix)) {
        session.agent.abort();
        session.agent.reset();
        sessions.delete(key);
      }
    }
    return;
  }
  if (command.type === "run") {
    try {
      await run(command);
    } catch (error) {
      emit({ type: "run_finished", run_id: command.run_id, ok: false, error: String(error?.message || error) });
    }
  }
}

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  try {
    const command = JSON.parse(line);
    // A tool response or cancellation must be processed while run() is
    // awaiting the matching tool promise, so only run itself is detached.
    if (command.type === "run") void handle(command);
    else await handle(command);
  } catch (error) {
    emit({ type: "protocol_error", error: String(error?.message || error) });
  }
}
