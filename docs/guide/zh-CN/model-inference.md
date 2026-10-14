# 本地与远程模型推理

Hitch 接入已经部署的模型 API。多个 run、eval 和 daemon 可以共享端点。Hitch 负责任务并发、期限、取消和证据；模型加载、副本、GPU 与服务生命周期由部署所有者管理。

## 注册与验证端点

连接文件只保存 URL、协议、上游模型名、认证引用与能力声明。`base_url` 保留路径前缀，末尾追加 `responses` 或 `chat/completions`，不重复插入 `/v1`。

```json
{
  "schema_version": "1",
  "base_url": "http://127.0.0.1:30000/v1",
  "wire_model": "coder-v1",
  "api": "responses",
  "auth": { "kind": "env", "name": "CODER_API_KEY" },
  "capabilities": {
    "streaming": true, "tool_calls": true,
    "parallel_tool_calls": false, "input_modalities": ["text"]
  }
}
```

无认证服务显式使用 `{"kind":"none"}`。密钥值只放在实际访问上游的进程环境中；daemon 必须在启动时取得该环境，另一个终端新 export 的变量不会更新已运行的 daemon。

```bash
hitch models connect --name coder --file ENDPOINT.json
hitch models inspect endpoint/coder --verify --json
hitch run --harness codex@installed --model endpoint/coder \
  --max-output-tokens 4096 --prompt "总结这个项目" --output json
```

connect 只保存连接，不下载权重或启动模型服务。verify 按声明执行有限的真实文本、SSE，以及工具调用→结果回传→模型回答验证；首次提交会验证尚未验证的 revision。验证本身会产生普通 API 用量，与任务用量分开。模型名回显不证明已加载特定权重。

## 当前支持的 Harness

| Harness | 协议 | 范围 |
| --- | --- | --- |
| Codex | Responses | 文本、完整历史、SSE、Harness 函数工具与 usage；私有 provider 配置关闭请求和流自动重试 |
| model-call | Responses 或 Chat Completions | 单次文本请求，无工具执行，无 Python 依赖 |
| training-tool | Chat Completions | Harbor 内普通候选评测的线性 shell 工具循环，不要求训练 policy lease 或精确训练采集 |

不兼容组合启动前失败。首版只接受 `parallel_tool_calls=false`。服务端会话、previous_response_id、后台 Responses、内置联网工具及多模态端点不属于此矩阵。原有 Harness 的原生 provider 配置仍可独立使用。

正常预算如 1024、4096 原样传递，不新增补偿或人为最小值。极小预算与上游少量 token 预留差异不作为交付阻塞项。

## 冻结提交与恢复

注册配置具有内容摘要 revision。重新连接别名只影响新任务；已提交的 run、排队 eval、修复重跑和 daemon 恢复保留原 `endpoint_binding`、协议、wire model 与生成参数。也可使用 `endpoint/coder@sha256:REVISION` 明确固定引用。

连接 URL 和认证引用保存于私有 registry。公共 request、plan、work spec 和 result 仅保存绑定投影。现有 TypeScript 任务代理负责鉴权、转发、取消和采集，不加载、调度或停止模型服务。代理在控制端或 Worker 解析凭据，Harness 容器只取得任务访问能力。

Gear 使用 `--model-binding-file READY.json` 提交就绪绑定：公共字段附加输入专用的 `connection`，以及可选 `deployment_receipt`。revision 为规范化的 schema_version/base_url/wire_model/api/auth/capabilities 摘要；生成参数单独冻结。导入文件保存不可变 revision，不修改别名。

公开回执的 service_config 只接受 engine/version/digest、量化、解析器、tokenizer/template 摘要、缓存、隔离和 parsing 配置；连接 URL、秘密字段与候选路由身份不能混入公共条件。严格候选证据由 Gear 校验可信回执来源与不可变 HF snapshot digest。

## Harbor 与远程 Worker

完成[评测准备](evaluations.md)后使用相同模型引用：

```bash
hitch eval run --daemon --dataset DATASET --harness codex@version:VERSION \
  --model endpoint/coder --max-output-tokens 4096 --max-concurrent 16 --output json
```

当前 Harbor 路径复用控制端/Worker 转发通道。远程 Worker 声明 `external_endpoint_binding=1`；精确训练继续使用 `training_external_binding=2`。容器中的 127.0.0.1 指向容器自身，因此必须分别验证上游可达性和容器到任务代理的可达性。不会自动转换协议、回退模型或部署引擎。

## 并发、取消与计量

`--max-concurrent` 是 Agent/trial 数，生成队列与副本负载由上游管理。外部模型服务不占 Hitch 控制端 GPU 配额；任务工具本身需要 GPU 时仍按任务资源合同处理。

完成、取消、超时与 daemon 退出只结束自己的请求和任务执行，不调用共享服务 stop/drain/flush，不把任务结束当作物理 GPU 释放。流消费者断开会撤销其上游请求。转发不增加自动重试，已有显式 attempt 保留证据。

usage 保留真实字段来源，累计摘要归一化 input/prompt 和 output/completion tokens。缺失字段保持未知，共享推理 GPU seconds 不填 0。重跑用量包含保留的无效与替换 run 的 publication，并按 interaction ID 去重。

## 上游部署与历史读取

固定版本的[单副本、TP=2 和官方 Gateway 双副本示例](../../../examples/external-inference/README.md)显式配置队列与并发限制；首版训练和推理使用静态分开的 GPU。示例包含正常预算验收探针与未执行项明确标注的实机清单，CPU 契约测试不能替代目标硬件验收。

保留 models add、本地模型 manifest/CAS 校验及历史推理 schema。普通 Python 推理包、local 服务命令、模型节点 RPC 与 models add-node 已退出。旧任务不能静默改绑新端点恢复；试用环境先用旧版本工具停止旧服务，再移除环境。需要重新评测时创建关联历史记录的新任务。
