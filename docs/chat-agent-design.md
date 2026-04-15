# Chat-Agent 设计方案（TypeScript LangGraph 版）

> **参考来源**：基于 [Townsend-Z/deer-flow](https://github.com/Townsend-Z/deer-flow) 的 Leader Agent 架构，提炼核心设计模式，使用 TypeScript 版 LangGraph/LangChain 重新设计。

---

## 一、DeerFlow 架构精要提炼

从源码分析中提取的关键设计模式：

| DeerFlow 组件 | 核心设计 | 本方案对标 |
|---|---|---|
| `make_lead_agent` | 配置驱动的 Agent 工厂，按 `RunnableConfig` 动态组装 model/tools/middleware | `createLeaderAgent()` 工厂函数 |
| `factory.py` → `_assemble_from_features` | **14 层中间件链**按固定顺序拼装，`RuntimeFeatures` 声明式开关 | 简化为 4-5 层核心中间件 |
| `SummarizationMiddleware` | `before_model` 钩子检测 token 数，超阈值自动压缩旧消息为 summary + 保留最近 N 条 | `ContextCompressor` 节点 |
| `MemoryMiddleware` | `after_agent` 钩子异步入队 → debounce → LLM 提取结构化记忆 (facts/context/history) | `MemoryManager` |
| `SubagentExecutor` | `ThreadPoolExecutor` 并发执行，`cancel_event` 协同取消，timeout 控制 | LangGraph `Send()` 并行分支 |
| `SubagentConfig` | 注册制：`name/description/system_prompt/tools/model/max_turns/timeout` | `SubAgentSpec` 类型定义 |
| `SubagentLimitMiddleware` | `after_model` 截断超额 task tool_call（硬限制 2-4） | `planNode` 内置限制 |
| `ThreadState` | 扩展 `AgentState`，附加 sandbox/title/artifacts/todos/viewed_images | `ChatState` 类型 |

---

## 二、整体架构图

```
┌──────────────────────────────────────────────────────────┐
│                      User Input                          │
└──────────────┬───────────────────────────────────────────┘
               ▼
┌──────────────────────────────────────────────────────────┐
│                   LEADER AGENT GRAPH                     │
│                                                          │
│  ┌─────────┐    ┌���─────────────┐    ┌──────────────┐    │
│  │ Memory  │───▶│   Context    │───▶│   Leader     │    │
│  │ Inject  │    │  Compressor  │    │   Planner    │    │
│  └─────────┘    └──────────────┘    └──────┬───────┘    │
│                                            │             │
│                    ┌───────────────────────┐│             │
│                    │  Route Decision       ││             │
│                    │  ┌─direct_answer──────┘│             │
│                    │  │spawn_subagents      │             │
│                    │  └─────┬───────────────┘             │
│                    │        ▼                             │
│            ┌───────────────────────────────┐             │
│            │   PARALLEL SUB-AGENT FANOUT   │             │
│            │                               │             │
│            │  ┌─────────┐  ┌─────────┐    │             │
│            │  │SubAgent │  │SubAgent │... │             │
│            │  │  (A)    │  │  (B)    │    │             │
│            │  └────┬────┘  └────┬────┘    │             │
│            │       └─────┬──────┘         │             │
│            └─────────────┼────────────────┘             │
│                          ▼                               │
│                 ┌──────────────┐                         │
│                 │  Synthesizer │                         │
│                 │  (汇总结果)   │                         │
│                 └──────────────┘                         │
└──────────────────────────────────────────────────────────┘
```

---

## 三、核心类型定义

### 3.1 主 Graph State

> 对标 DeerFlow 的 `ThreadState`（`backend/packages/harness/deerflow/agents/thread_state.py`）

```typescript
// src/types/state.ts

import { Annotation, messagesStateReducer } from "@langchain/langgraph";
import type { BaseMessage } from "@langchain/core/messages";

/** 子 Agent 执行计划 */
export interface SubAgentTask {
  taskId: string;
  agentName: string;         // 对应注册的 subagent spec
  instruction: string;       // leader 给子 agent 的具体指令
  tools?: string[];          // 可使用的 tool 白名单
  maxTurns?: number;
  timeoutMs?: number;
}

/** 子 Agent 执行结果 */
export interface SubAgentResult {
  taskId: string;
  agentName: string;
  status: "completed" | "failed" | "timeout" | "cancelled";
  result?: string;
  error?: string;
  durationMs?: number;
}

/** 长期记忆结构 (对标 DeerFlow 的 memory/storage.py) */
export interface LongTermMemory {
  version: string;
  lastUpdated: string;
  user: {
    workContext: { summary: string; updatedAt: string };
    personalContext: { summary: string; updatedAt: string };
    topOfMind: { summary: string; updatedAt: string };
  };
  facts: Array<{ content: string; createdAt: string; source: string }>;
}

/** 短期记忆：当前会话的上下文摘要 */
export interface ShortTermMemory {
  conversationSummary: string;     // 被压缩掉的历��的摘要
  summaryTokenCount: number;
  lastSummarizedAt?: string;
}

/** 主 Graph State */
export const ChatState = Annotation.Root({
  messages: Annotation<BaseMessage[]>({
    reducer: messagesStateReducer,
  }),

  // 记忆
  longTermMemory: Annotation<LongTermMemory | null>({
    reducer: (_, next) => next,
    default: () => null,
  }),
  shortTermMemory: Annotation<ShortTermMemory | null>({
    reducer: (_, next) => next,
    default: () => null,
  }),

  // 规划
  plan: Annotation<SubAgentTask[]>({
    reducer: (_, next) => next,
    default: () => [],
  }),

  // 子 Agent 结果收集
  subAgentResults: Annotation<SubAgentResult[]>({
    reducer: (existing, incoming) => [...existing, ...incoming],
    default: () => [],
  }),

  // 元信息
  title: Annotation<string | null>({
    reducer: (_, next) => next,
    default: () => null,
  }),
});

export type ChatStateType = typeof ChatState.State;
```

### 3.2 子 Agent 注册配置

> 对标 DeerFlow 的 `SubagentConfig`（`backend/packages/harness/deerflow/subagents/config.py`）

```typescript
// src/types/subagent-spec.ts

export interface SubAgentSpec {
  name: string;
  description: string;           // leader 决策时参考的描述
  systemPrompt: string;
  tools?: string[];              // 允许使用的 tool 名称
  disallowedTools?: string[];    // 禁止使用的 tool 名称
  model?: string;                // "inherit" | 具体模型名
  maxTurns: number;              // 默认 25
  timeoutMs: number;             // 默认 120_000
}
```

---

## 四、核心模块详解

### 4.1 上下文自动压缩 (Context Compressor)

> **对标 DeerFlow**：`SummarizationMiddleware`（`backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py`）
>
> DeerFlow 的实现是在 `before_model` 阶段检查 message token 总数，超过阈值则按 cutoff_index 将旧消息 LLM 摘要，然后用 `RemoveMessage(id=REMOVE_ALL_MESSAGES)` 替换整个历史为 `[summary, ...preserved_messages]`。

```typescript
// src/nodes/context-compressor.ts

import { ChatOpenAI } from "@langchain/openai";
import { HumanMessage, SystemMessage, RemoveMessage } from "@langchain/core/messages";
import type { ChatStateType } from "../types/state";

const COMPRESSION_THRESHOLD = 8000;    // tokens
const PRESERVE_RECENT = 6;            // 保留最近 N 条 message

/**
 * 上下文压缩节点
 *
 * 策略���
 * 1. 估算 messages 总 token 数
 * 2. 超阈值 → 将旧消息 LLM 摘要为一条 SystemMessage
 * 3. 保留最近 N 条原始 message + summary
 * 4. 同时更新 shortTermMemory
 */
export async function contextCompressorNode(
  state: ChatStateType
): Promise<Partial<ChatStateType>> {
  const { messages } = state;
  const tokenCount = estimateTokens(messages);

  if (tokenCount < COMPRESSION_THRESHOLD || messages.length <= PRESERVE_RECENT) {
    return {}; // 不需要压缩
  }

  const cutoffIndex = messages.length - PRESERVE_RECENT;
  const messagesToSummarize = messages.slice(0, cutoffIndex);
  const preservedMessages = messages.slice(cutoffIndex);

  // LLM 压缩
  const summaryModel = new ChatOpenAI({
    modelName: "gpt-4o-mini",
    temperature: 0,
  });

  const summaryResponse = await summaryModel.invoke([
    new SystemMessage(
      `You are a conversation summarizer. Summarize the following conversation ` +
      `preserving all key facts, decisions, user preferences, and pending tasks. ` +
      `Be concise but thorough. Output in the user's language.`
    ),
    new HumanMessage(
      messagesToSummarize.map(m => `[${m._getType()}]: ${m.content}`).join("\n")
    ),
  ]);

  const summary = typeof summaryResponse.content === "string"
    ? summaryResponse.content
    : String(summaryResponse.content);

  return {
    messages: [
      ...messagesToSummarize.map(m => new RemoveMessage({ id: m.id! })),
      new SystemMessage(`[Conversation Summary]\n${summary}`),
    ],
    shortTermMemory: {
      conversationSummary: summary,
      summaryTokenCount: estimateTokens([summaryResponse]),
      lastSummarizedAt: new Date().toISOString(),
    },
  };
}

function estimateTokens(messages: any[]): number {
  return messages.reduce((sum, m) => {
    const content = typeof m.content === "string" ? m.content : JSON.stringify(m.content);
    return sum + Math.ceil(content.length / 3);
  }, 0);
}
```

### 4.2 长短期记忆系统 (Memory Manager)

> **对标 DeerFlow**：
> - `MemoryMiddleware`（`backend/packages/harness/deerflow/agents/middlewares/memory_middleware.py`）— after_agent → queue.add
> - `memory/updater.py` — LLM 提取 facts/context
> - `memory/storage.py` — JSON 文件存储 + `MemoryStorage` 抽象基类

```typescript
// src/memory/memory-manager.ts

import { ChatOpenAI } from "@langchain/openai";
import { HumanMessage, SystemMessage } from "@langchain/core/messages";
import type { LongTermMemory } from "../types/state";

/** 记忆存储接口 (对标 DeerFlow MemoryStorage ABC) */
export interface MemoryStore {
  load(userId: string): Promise<LongTermMemory | null>;
  save(userId: string, memory: LongTermMemory): Promise<void>;
}

/** 简单内存存储实现 */
export class InMemoryStore implements MemoryStore {
  private store = new Map<string, LongTermMemory>();

  async load(userId: string) {
    return this.store.get(userId) ?? null;
  }

  async save(userId: string, memory: LongTermMemory) {
    this.store.set(userId, memory);
  }
}

const MEMORY_UPDATE_PROMPT = `You are a memory extraction assistant.
Given the existing user memory and a new conversation, update the memory by:
1. Updating workContext/personalContext/topOfMind summaries if new info emerges
2. Extracting new facts (preferences, decisions, constraints)
3. Never remove existing facts unless explicitly contradicted
4. Output valid JSON matching the LongTermMemory schema.`;

/**
 * 记忆管理器
 * - inject: 读取长期记忆注入到 system prompt
 * - update: 对话结束后异步提取记忆并持久化
 */
export class MemoryManager {
  private store: MemoryStore;
  private model: ChatOpenAI;

  constructor(
    store: MemoryStore,
    model = new ChatOpenAI({ modelName: "gpt-4o-mini", temperature: 0 }),
  ) {
    this.store = store;
    this.model = model;
  }

  /** 加载记忆 (用于 memoryInjectNode) */
  async load(userId: string): Promise<LongTermMemory | null> {
    return this.store.load(userId);
  }

  /** 异步更新记忆 (用于 memorySaveNode) */
  async update(
    userId: string,
    conversationText: string,
    existing: LongTermMemory | null,
  ): Promise<LongTermMemory> {
    const existingJson = existing ? JSON.stringify(existing, null, 2) : "null";

    const response = await this.model.invoke([
      new SystemMessage(MEMORY_UPDATE_PROMPT),
      new HumanMessage(
        `Existing memory:\n${existingJson}\n\nNew conversation:\n${conversationText}`
      ),
    ]);

    try {
      const content = typeof response.content === "string"
        ? response.content : String(response.content);
      const updated: LongTermMemory = JSON.parse(
        content.replace(/```json\n?/g, "").replace(/```/g, "").trim()
      );
      updated.lastUpdated = new Date().toISOString();
      await this.store.save(userId, updated);
      return updated;
    } catch {
      return existing ?? createEmptyMemory();
    }
  }
}

export function createEmptyMemory(): LongTermMemory {
  return {
    version: "1.0",
    lastUpdated: new Date().toISOString(),
    user: {
      workContext: { summary: "", updatedAt: "" },
      personalContext: { summary: "", updatedAt: "" },
      topOfMind: { summary: "", updatedAt: "" },
    },
    facts: [],
  };
}
```

### 4.3 Leader Agent 规划节点

> **对标 DeerFlow**：
> - `make_lead_agent`（`backend/packages/harness/deerflow/agents/lead_agent/agent.py`）
> - `lead_agent/prompt.py` — 动态生成 system prompt，注入可用 skills/subagent 描述
> - `SubagentLimitMiddleware` — 硬限制并发数

```typescript
// src/nodes/leader-planner.ts

import { ChatOpenAI } from "@langchain/openai";
import { SystemMessage, AIMessage } from "@langchain/core/messages";
import { z } from "zod";
import type { ChatStateType, SubAgentTask } from "../types/state";
import type { SubAgentSpec } from "../types/subagent-spec";
import { getRegisteredSpecs } from "../registry/subagent-registry";

const MAX_PARALLEL_SUBAGENTS = 3;  // 对标 DeerFlow MAX_CONCURRENT_SUBAGENTS

const PlanSchema = z.object({
  thinking: z.string().describe("Your analysis of the task"),
  decision: z.enum(["direct_answer", "spawn_subagents"]),
  directAnswer: z.string().optional(),
  tasks: z.array(z.object({
    agentName: z.string(),
    instruction: z.string(),
    tools: z.array(z.string()).optional(),
  })).optional(),
});

function buildSystemPrompt(specs: SubAgentSpec[], memoryContext: string): string {
  const specDescriptions = specs
    .map(s => `- **${s.name}**: ${s.description}`)
    .join("\n");

  return `You are a Leader Agent that coordinates specialized sub-agents.

## Available Sub-Agents\n${specDescriptions}\n
## User Memory Context\n${memoryContext || "(no prior memory)"}\n
## Rules\n1. For simple questions, choose "direct_answer" and respond immediately.\n2. For complex multi-step tasks, choose "spawn_subagents" and create a task plan.\n3. Maximum ${MAX_PARALLEL_SUBAGENTS} concurrent sub-agents.\n4. Each task instruction must be self-contained (sub-agents have NO shared context).\n5. Assign the most appropriate sub-agent based on its description.\n6. Output valid JSON matching the plan schema.`;
}

export async function leaderPlannerNode(
  state: ChatStateType
): Promise<Partial<ChatStateType>> {
  const specs = getRegisteredSpecs();
  const memoryContext = state.longTermMemory
    ? JSON.stringify(state.longTermMemory.user)
    : "";

  const model = new ChatOpenAI({
    modelName: "gpt-4o",
    temperature: 0,
  }).withStructuredOutput(PlanSchema);

  const response = await model.invoke([
    new SystemMessage(buildSystemPrompt(specs, memoryContext)),
    ...state.messages,
  ]);

  if (response.decision === "direct_answer") {
    return {
      messages: [new AIMessage(response.directAnswer ?? "")],
      plan: [],
    };
  }

  // 限制并发数 (对标 SubagentLimitMiddleware)
  const rawTasks = response.tasks ?? [];
  const tasks: SubAgentTask[] = rawTasks
    .slice(0, MAX_PARALLEL_SUBAGENTS)
    .map((t, i) => ({
      taskId: `task-${Date.now()}-${i}`,
      agentName: t.agentName,
      instruction: t.instruction,
      tools: t.tools,
    }));

  return { plan: tasks };
}

/** 路由函数：根据 plan 决定走直接回答还是并行子 Agent */
export function routeAfterPlan(
  state: ChatStateType
): "synthesizer" | "subAgentFanout" {
  return state.plan.length === 0 ? "synthesizer" : "subAgentFanout";
}
```

### 4.4 子 Agent 并行执行

> **对标 DeerFlow**：`SubagentExecutor`（`backend/packages/harness/deerflow/subagents/executor.py`）
>
> DeerFlow 使用 `ThreadPoolExecutor` + `asyncio.run` + timeout + `cancel_event` 协同取消。
> 本方案使用 LangGraph TS 的 `Send()` API 实现动态并行分支。

```typescript
// src/nodes/subagent-executor.ts

import { Send } from "@langchain/langgraph";
import { ChatOpenAI } from "@langchain/openai";
import { HumanMessage, SystemMessage } from "@langchain/core/messages";
import { createReactAgent } from "@langchain/langgraph/prebuilt";
import type { ChatStateType, SubAgentResult } from "../types/state";
import { getSubAgentSpec } from "../registry/subagent-registry";
import { getToolsByNames } from "../registry/tool-registry";

/** 执行单个子 Agent (对标 DeerFlow SubagentExecutor._aexecute) */
async function executeSubAgent(
  task: { taskId: string; agentName: string; instruction: string; tools?: string[] }
): Promise<SubAgentResult> {
  const spec = getSubAgentSpec(task.agentName);
  if (!spec) {
    return {
      taskId: task.taskId,
      agentName: task.agentName,
      status: "failed",
      error: `Unknown sub-agent: ${task.agentName}`,
    };
  }

  const startTime = Date.now();
  const tools = getToolsByNames(task.tools ?? spec.tools ?? []);

  const model = new ChatOpenAI({
    modelName: spec.model === "inherit" ? "gpt-4o" : spec.model,
    temperature: 0,
  });

  const agent = createReactAgent({
    llm: model,
    tools,
    messageModifier: new SystemMessage(spec.systemPrompt),
  });

  try {
    // Timeout 控制 (对标 DeerFlow executor timeout_seconds)
    const result = await Promise.race([
      agent.invoke({
        messages: [new HumanMessage(task.instruction)],
      }),
      new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error("timeout")), spec.timeoutMs)
      ),
    ]);

    const lastMsg = result.messages[result.messages.length - 1];
    return {
      taskId: task.taskId,
      agentName: task.agentName,
      status: "completed",
      result: typeof lastMsg.content === "string"
        ? lastMsg.content
        : JSON.stringify(lastMsg.content),
      durationMs: Date.now() - startTime,
    };
  } catch (err: any) {
    return {
      taskId: task.taskId,
      agentName: task.agentName,
      status: err.message === "timeout" ? "timeout" : "failed",
      error: err.message,
      durationMs: Date.now() - startTime,
    };
  }
}

/**
 * Fanout 节点：将 plan 中的 tasks 通过 Send() 分发到并���子 Agent
 * 对标 DeerFlow 的 task_tool → execute_async → ThreadPoolExecutor
 */
export function subAgentFanoutNode(state: ChatStateType): Send[] {
  return state.plan.map(
    (task) => new Send("subAgentWorker", { task })
  );
}

/** 子 Agent Worker 节点：执行单个 task 并返回结果 */
export async function subAgentWorkerNode(
  input: { task: ChatStateType["plan"][0] }
): Promise<Partial<ChatStateType>> {
  const result = await executeSubAgent(input.task);
  return {
    subAgentResults: [result],
  };
}
```

### 4.5 结果汇总节点 (Synthesizer)

```typescript
// src/nodes/synthesizer.ts

import { ChatOpenAI } from "@langchain/openai";
import { AIMessage, HumanMessage, SystemMessage } from "@langchain/core/messages";
import type { ChatStateType } from "../types/state";

const SYNTHESIS_PROMPT = `You are a result synthesizer. You will receive results from multiple specialized sub-agents.

Rules:
1. Combine all sub-agent results into a coherent, unified response.
2. If any sub-agent failed/timed out, acknowledge what was missed and provide what you can.
3. Preserve the user's language.
4. Structure the response clearly if multiple distinct topics were handled.
5. Do NOT mention internal sub-agent names or task IDs to the user.`;

export async function synthesizerNode(
  state: ChatStateType
): Promise<Partial<ChatStateType>> {
  const { subAgentResults, messages } = state;

  // 如果没有子 agent 结果（直接回答路径），直接返回
  if (!subAgentResults || subAgentResults.length === 0) {
    return {};
  }

  const resultsText = subAgentResults
    .map((r) => {
      if (r.status === "completed") {
        return `[${r.agentName}] ✅ Result:\n${r.result}`;
      }
      return `[${r.agentName}] ❌ ${r.status}: ${r.error ?? "unknown error"}`;
    })
    .join("\n\n---\n\n");

  const model = new ChatOpenAI({ modelName: "gpt-4o", temperature: 0.1 });

  const response = await model.invoke([
    new SystemMessage(SYNTHESIS_PROMPT),
    ...messages,
    new HumanMessage(`Sub-agent results:\n\n${resultsText}`),
  ]);

  return {
    messages: [new AIMessage(
      typeof response.content === "string"
        ? response.content
        : String(response.content)
    )],
    plan: [],
    subAgentResults: [],
  };
}
```

### 4.6 Graph 组装

```typescript
// src/graph/chat-agent-graph.ts

import { StateGraph, END, START } from "@langchain/langgraph";
import { MemorySaver } from "@langchain/langgraph";
import { SystemMessage } from "@langchain/core/messages";
import { ChatState } from "../types/state";
import { contextCompressorNode } from "../nodes/context-compressor";
import { leaderPlannerNode, routeAfterPlan } from "../nodes/leader-planner";
import { subAgentFanoutNode, subAgentWorkerNode } from "../nodes/subagent-executor";
import { synthesizerNode } from "../nodes/synthesizer";

/**
 * 组装主 Graph (对标 DeerFlow make_lead_agent → create_agent)
 *
 * 节点流程：
 *   memoryInject → contextCompressor → leaderPlanner
 *     ──[direct_answer]──▶ synthesizer → memorySave → END
 *     ──[spawn_subagents]──▶ subAgentFanout → subAgentWorker(并行)
 *                            → synthesizer → memorySave → END
 */
export function buildChatAgentGraph() {
  const graph = new StateGraph(ChatState)

    // 1. 注入长期记忆
    .addNode("memoryInject", memoryInjectNode)

    // 2. 上下文压缩
    .addNode("contextCompressor", contextCompressorNode)

    // 3. Leader 规划
    .addNode("leaderPlanner", leaderPlannerNode)

    // 4. 子 Agent 并行 fan-out
    .addNode("subAgentFanout", subAgentFanoutNode)

    // 5. 子 Agent Worker (接收 Send)
    .addNode("subAgentWorker", subAgentWorkerNode)

    // 6. 汇总
    .addNode("synthesizer", synthesizerNode)

    // 7. 保存记忆
    .addNode("memorySave", memorySaveNode)

    // 边
    .addEdge(START, "memoryInject")
    .addEdge("memoryInject", "contextCompressor")
    .addEdge("contextCompressor", "leaderPlanner")
    .addConditionalEdges("leaderPlanner", routeAfterPlan, {
      synthesizer: "synthesizer",
      subAgentFanout: "subAgentFanout",
    })
    .addEdge("subAgentWorker", "synthesizer")
    .addEdge("synthesizer", "memorySave")
    .addEdge("memorySave", END);

  return graph.compile({
    checkpointer: new MemorySaver(),
  });
}

// --- 辅助节点 ---

async function memoryInjectNode(state: typeof ChatState.State) {
  if (!state.longTermMemory) return {};

  const { user, facts } = state.longTermMemory;
  const memoryText = [
    user.topOfMind.summary && `Current focus: ${user.topOfMind.summary}`,
    user.workContext.summary && `Work context: ${user.workContext.summary}`,
    facts.length > 0 &&
      `Known facts:\n${facts.map(f => `- ${f.content}`).join("\n")}`,
  ].filter(Boolean).join("\n\n");

  if (!memoryText) return {};

  return {
    messages: [new SystemMessage(`[User Memory]\n${memoryText}`)],
  };
}

async function memorySaveNode(state: typeof ChatState.State) {
  // 异步触发记忆更新（不阻塞响应）
  // 实际实现中通过 MemoryManager.update() 后台执行
  return {};
}
```

---

## 五、子 Agent 注册表

> 对标 DeerFlow 的 `subagents/builtins` + `subagents/registry.py`

```typescript
// src/registry/subagent-registry.ts

import type { SubAgentSpec } from "../types/subagent-spec";

const REGISTRY = new Map<string, SubAgentSpec>();

export function registerSubAgent(spec: SubAgentSpec): void {
  REGISTRY.set(spec.name, spec);
}

export function getSubAgentSpec(name: string): SubAgentSpec | undefined {
  return REGISTRY.get(name);
}

export function getRegisteredSpecs(): SubAgentSpec[] {
  return Array.from(REGISTRY.values());
}

// --- 内置 Sub-Agents ---

registerSubAgent({
  name: "researcher",
  description:
    "Searches the web and synthesizes information for research-heavy questions.",
  systemPrompt: `You are a research specialist. Use search tools to find accurate, up-to-date information.
Always cite sources. Summarize findings clearly.`,
  tools: ["web_search", "url_reader"],
  model: "inherit",
  maxTurns: 15,
  timeoutMs: 120_000,
});

registerSubAgent({
  name: "coder",
  description:
    "Writes, reviews, and explains code. Handles programming tasks.",
  systemPrompt: `You are a senior software engineer. Write clean, well-documented code.
Explain your approach before writing code. Handle edge cases.`,
  tools: ["code_sandbox", "file_reader"],
  model: "inherit",
  maxTurns: 25,
  timeoutMs: 180_000,
});

registerSubAgent({
  name: "writer",
  description:
    "Creates, edits, and refines written content (articles, reports, emails).",
  systemPrompt: `You are a professional writer. Create high-quality content matching the user's style and requirements.
Pay attention to structure, clarity, and tone.`,
  tools: [],
  model: "inherit",
  maxTurns: 10,
  timeoutMs: 60_000,
});
```

---

## 六、分阶段实施路线图

### Phase 1：最小可用对话链路 (MVP) — 2 周

**目标**：Leader → Plan → 并行子 Agent → 汇总回答

| 模块 | 内容 | 对标 DeerFlow |
|---|---|---|
| `ChatState` | 定义核心 state 类型 | `ThreadState` |
| `leaderPlannerNode` | 接收用户输入，structured output 生成 plan | `make_lead_agent` |
| `subAgentFanoutNode` | `Send()` 分发并行任务 | `SubagentExecutor.execute_async` |
| `subAgentWorkerNode` | `createReactAgent` 执行单个任务 + timeout | `SubagentExecutor._aexecute` |
| `synthesizerNode` | LLM 汇总所有子 agent 结果 | （DeerFlow 在 lead agent prompt 中隐式完成） |
| `subagent-registry` | 注册 2-3 个内置子 agent | `subagents/builtins` + `registry.py` |
| Graph 组装 | `StateGraph` + `MemorySaver` | `langgraph.json` + `checkpointer` |

**交付物**：可运行的 CLI demo，输入复杂问题 → 自动拆分 → 并行执行 → 合并回答。

---

### Phase 2：上下文压缩 + 短期记忆 — 1 周

**目标**：长对话不爆 context window

| 模块 | 内容 | 对标 DeerFlow |
|---|---|---|
| `contextCompressorNode` | token 估算 → 超阈值 LLM 摘要 → `RemoveMessage` | `SummarizationMiddleware.before_model` |
| `ShortTermMemory` | 存储压缩后的 summary | `SummarizationMiddleware` 内部状态 |
| 子 Agent 压缩 | 子 Agent 独立 message 流也做压缩（max_turns 限制 + 压缩） | `SubagentConfig.max_turns` |

**交付物**：50+ 轮对话不截断，token 用量稳定在阈值以下。

---

### Phase 3：长期记忆系统 — 1.5 周

**目标**：跨会话记忆持久化

| 模块 | 内容 | 对标 DeerFlow |
|---|---|---|
| `MemoryStore` 接口 | 抽象存储层（内存/文件/DB） | `MemoryStorage` ABC |
| `MemoryManager` | LLM 提取 facts/context → 结构化存储 | `memory/updater.py` |
| `memoryInjectNode` | 会话开始时注入长期记忆 | `MemoryMiddleware` + `memory/prompt.py` |
| `memorySaveNode` | 会话结束时异步更新记忆 | `MemoryMiddleware.after_agent` → queue → updater |
| `summarization_hook` | 压缩前 flush 待更新记忆 | `memory/summarization_hook.py` |

**交付物**：重启后记住用户偏好、历史决策。

---

### Phase 4：生产化加固 — 1.5 周

| 模块 | 内容 | 对标 DeerFlow |
|---|---|---|
| Tool 注册表 | 可扩展的 tool 注册 + 白名单/黑名单过滤 | `tools/builtins` + `_filter_tools()` |
| 错误处理中间件 | tool 异常捕获 → 友好错误消息 | `ToolErrorHandlingMiddleware` |
| 循环检测 | 子 Agent 重复 tool call 检测 + 强制终止 | `LoopDetectionMiddleware` |
| Cancel 机制 | 子 Agent 超时/用户取消的协同停止 | `SubagentResult.cancel_event` |
| 可观测性 | LangSmith tracing / 自定义 trace | `tracing/` + metadata injection |
| API 层 | Express/Fastify gateway + SSE streaming | `gateway/` FastAPI |

---

## 七、关键设计决策对比

| 决策点 | DeerFlow (Python) | 本方案 (TypeScript) | 理由 |
|---|---|---|---|
| 并发模型 | `ThreadPoolExecutor` + `asyncio.run` 隔离事件循环 | LangGraph `Send()` 原生并行 | TS 单线程，`Send()` 更自然 |
| 中间件 vs 节点 | 14 层 `AgentMiddleware` 链 | Graph 节点 + 条件边 | TS 版 LangGraph 中间件 API 较新，节点更直观 |
| 记忆更新 | debounce queue → 异步 LLM 提取 | `memorySaveNode` 异步触发 | 简化，Phase 4 可加 debounce |
| 上下文压缩触发 | `before_model` hook，每轮检查 | 独立 `contextCompressorNode` | 职责分离更清晰 |
| 子 Agent 创建 | `create_agent` 运行时动态创建 | `createReactAgent` 按 spec 动态创建 | 等价实现 |
| 状态管理 | `ThreadState` + LangGraph checkpointer | `ChatState` Annotation + `MemorySaver` | 等价实现 |

---

## 八、快速启动示例

```typescript
// src/index.ts

import { buildChatAgentGraph } from "./graph/chat-agent-graph";
import { HumanMessage } from "@langchain/core/messages";

async function main() {
  const graph = buildChatAgentGraph();

  const threadId = "user-session-001";

  const result = await graph.invoke(
    {
      messages: [
        new HumanMessage(
          "帮我研究一下 2025 年 AI Agent 框架的发展趋势，同时写一段 Python demo 展示 LangGraph 的基本用法"
        ),
      ],
    },
    {
      configurable: { thread_id: threadId },
    }
  );

  console.log(
    "Final response:",
    result.messages[result.messages.length - 1].content
  );
}

main();
```

> **预期行为**：Leader 自动将此请求拆分为两个并行子任务——`researcher`（研究趋势）+ `coder`（写 demo），分别执行后由 `synthesizerNode` 合并为一个统一的回复。

---

## 九、项目目录结构

```
src/
├── types/
│   ├── state.ts              # ChatState, SubAgentTask, SubAgentResult, Memory 类型
│   └── subagent-spec.ts      # SubAgentSpec 定义
├── nodes/
│   ├── context-compressor.ts  # 上下文自动压缩节点
│   ├── leader-planner.ts      # Leader Agent 规划节点
│   ├── subagent-executor.ts   # 子 Agent 并行执行 (fanout + worker)
│   └── synthesizer.ts         # 结果汇总节点
├── memory/
│   └── memory-manager.ts      # 长短期记忆管理器 + 存储抽象
├── registry/
│   ├── subagent-registry.ts   # 子 Agent 注册表 + 内置 agents
│   └── tool-registry.ts       # Tool 注册表
├��─ graph/
│   └── chat-agent-graph.ts    # 主 Graph 组装
└── index.ts                   # 入口文件
```
