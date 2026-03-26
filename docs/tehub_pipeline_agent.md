# Tehub Data Pipeline Agent

`tehub-pipeline` 是一个专为 Tehub 数据接入平台设计的 DeerFlow 子代理（subagent），通过 Tehub MCP Server 的 26 个工具管理 Token、Stream 和 Task 的完整生命周期。

---

## 功能概览

- **Token 管理**：创建、查询、修改接入 Token
- **Stream 管理**：引导式创建数据流（含 Schema 采集）、配置草稿保存与发布、启动/停止/删除
- **Task 管理**：引导式创建入仓任务（含字段选择、过滤、转换、输出配置）、发布、启停
- **人工确认**：对所有状态变更操作（创建/发布/启停/删除/修改）强制要求用户确认
- **会话恢复**：自动检测未完成的草稿并提供恢复引导
- **最佳实践内嵌**：系统提示包含 Stream 类型选择、Schema 设计、版本管理等领域知识

---

## 快速开始

### 1. 启动 Tehub MCP Server

确保 `te-mcp-server-tehub` 在本地或网络可达地址运行：

```bash
# 默认地址（SSE 协议）
http://localhost:9001/mcp/sse/base/tehub
```

### 2. 配置 MCP Server 连接

复制并修改 `extensions_config.example.json` 为 `extensions_config.json`，启用 Tehub MCP Server：

```json
{
  "mcpServers": {
    "te-mcp-server-tehub": {
      "enabled": true,
      "type": "sse",
      "url": "http://localhost:9001/mcp/sse/base/tehub",
      "description": "Tehub data ingestion pipeline MCP server"
    }
  }
}
```

### 3. 验证安装

```bash
cd backend
make test
```

---

## 架构说明

### 文件结构

```
backend/packages/harness/deerflow/
├── subagents/builtins/
│   ├── tehub_pipeline_agent.py          # SubagentConfig（系统提示 + 工作流）
│   └── __init__.py                       # 注册 tehub-pipeline
└── agents/middlewares/
    └── tehub_confirmation_middleware.py  # 危险操作拦截 + 人工确认
```

### 主要组件

#### `TehubPipelineConfig`（`tehub_pipeline_agent.py`）

`SubagentConfig` 实例，包含：
- Agent 名称：`tehub-pipeline`
- 详细系统提示（中文），涵盖所有 26 个 MCP 工具的使用说明
- Stream 创建标准流程（5 步）
- Task 创建标准流程（5 步）
- Schema 采集三种方式
- 会话恢复协议
- 领域知识（Stream 类型、分发策略、Schema 设计最佳实践）
- `max_turns=100`，`timeout_seconds=1800`（30 分钟）

#### `TehubConfirmationMiddleware`（`tehub_confirmation_middleware.py`）

拦截危险 MCP 工具调用并触发人工确认：

```python
# 需要确认的工具（17 个）
TEHUB_DANGEROUS_TOOLS = {
    "createToken", "createStreamInfo", "createTaskInfo",
    "publishStreamConfig", "publishTaskConfigDraft",
    "startStream", "stopStream", "startTask", "stopTask", "killTask",
    "deleteStream", "deleteTask",
    "editTokenInfo", "editStreamInfo", "editTaskInfo",
    "editIsSchemaCheck", "updateStreamParkArgs",
}

# 只读工具，直接通过（7 个）
TEHUB_READONLY_TOOLS = {
    "getByTokenName", "getStreamEntityById", "getDraftStreamConfig",
    "getStreamConfigByVersion", "getStreamInfoPoByTokenId",
    "getTaskEntityById", "getTaskJobStartInfo",
}

# 草稿保存工具（2 个）：saveStreamConfigDraft、saveTaskConfigDraft
# 属于非破坏性写操作，无需人工确认（自动保存，可反复调用）
```

---

## 使用示例

### 场景 1：创建新的 Stream

```
用户：帮我创建一个用户行为事件流

Agent：好的，我来引导您完成 Stream 创建流程。
       首先，请问您是否已有 Token？如果有，请提供 Token 名称；如果没有，我可以帮您创建一个。

用户：我已有 Token，名字是 user_events_token

Agent：[调用 getByTokenName] 已找到 Token（tokenId=123）。
       接下来收集 Stream 基本信息：
       1. 请为 Stream 取一个名称（英文+数字+下划线格式）？

用户：user_behavior_stream

Agent：Stream 类型建议：
       - upsert：有主键的实体数据（用户、订单），支持更新
       - append：事件流、日志数据，只追加不更新
       您的用户行为事件属于哪种类型？

用户：append

...（后续引导收集其余字段和 Schema）...

Agent：⚠️ 即将执行：创建 Stream
       操作参数：
         - streamName: user_behavior_stream
         - streamType: append
         - ...
       请确认是否继续？

用户：确认

Agent：[调用 createStreamInfo] Stream 创建成功！streamId=456
       下一步：配置 Schema。您希望如何提供 Schema？
       A. 直接粘贴 JSON 数组
       B. 粘贴示例数据，我来推断
       C. 先保存草稿，稍后继续
```

### 场景 2：恢复中断的配置

```
用户：我之前在创建 stream，streamId 是 456，想继续完成

Agent：[调用 getDraftStreamConfig(streamId=456)]
       发现未完成的草稿！已收集信息：
       ✅ 基本信息（streamName, streamType, distributeStrategy）
       ❌ Schema（未配置）
       ❌ 配置未发布
       
       是否继续从 Schema 配置开始？
```

### 场景 3：停止运行中的任务

```
用户：停止 taskId=789 的任务

Agent：⚠️ 即将执行：停止 Task
       操作参数：
         - taskId: 789
       此操作将停止正在运行的入仓任务，数据处理将暂停。
       请确认是否继续？

用户：是

Agent：[调用 stopTask(taskId=789)] Task 已停止。
       如需重新启动，请告知我。
```

---

## 中间件使用说明

`TehubConfirmationMiddleware` 设计为可独立集成到任何使用 Tehub MCP 工具的 DeerFlow Agent：

```python
from deerflow.agents.middlewares.tehub_confirmation_middleware import (
    TehubConfirmationMiddleware,
    TEHUB_DANGEROUS_TOOLS,
    TEHUB_READONLY_TOOLS,
)

# 检查工具是否需要确认
tool_name = "deleteStream"
requires_confirmation = tool_name in TEHUB_DANGEROUS_TOOLS  # True

# 集成到自定义 agent（通过 AgentMiddleware 机制）
middleware = TehubConfirmationMiddleware()
```

---

## 测试

```bash
cd backend

# 运行 Tehub 相关测试
uv run pytest tests/test_tehub_confirmation_middleware.py tests/test_tehub_pipeline_agent.py -v

# 运行全部测试
make test
```

---

## 注意事项

1. **MCP Server 可用性**：Tehub MCP 工具依赖外部 MCP Server，若 Server 不可用，工具调用会失败。Agent 会提示用户检查连接。

2. **确认流程**：中间件在工具调用被执行**之前**拦截，向用户展示确认请求后中断执行（`goto=END`）。用户确认后需重新触发对话，Agent 会重新执行该工具。

3. **草稿自动保存**：Agent 系统提示中要求每收集到重要信息后即时保存草稿，但这依赖 LLM 遵循指令，建议在关键步骤后提醒用户手动触发保存。

4. **Schema 推断**：Schema 从示例数据推断的逻辑由 LLM 完成，对于复杂类型（嵌套 JSON、特殊格式）可能需要用户手动确认和调整。
