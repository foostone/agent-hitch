# 评估提交前固定产物

`evaluation_artifact_pins: "1"` 能力表示 local-docker 评估支持请求字段 `artifact_pins`。它与环境绑定分别约束依赖产物和模型环境，不通过版本号推断支持。

每个 pin 包含 `platform`、`artifact_id`、`artifact_integrity`、`entrypoint_integrity`；格式见 `schemas/eval-artifact-pin.schema.json`。只接受 Linux x64/ARM64，每个平台一条；请求必须精确覆盖本次任务所用平台。调用者应先用公开准备 API 取得真实产物，再将字段交给提交请求。CLI 用重复的 `--artifact-pin` 传入每条 pin 的 JSON；不接受目录作为身份。

请求规范化按平台排序，并将 pins 纳入持久 submission 和幂等摘要。新请求不能通过同一幂等键替换 pins。候选重启、准备恢复、重试继续使用原请求，不能重新解释为“接受当前缓存中的最新产物”。

两处执行检查：准备完成后核对全部平台与产物，再发布可执行计划；每次物理 Harbor 调用前核对当前实际选定的产物，包括重试和恢复。ID、文件摘要、入口摘要、平台任一不符都返回 `eval_artifact_pin_mismatch`，不会用另一份产物启动任务。准备所需的工具版本查询可以发生在该检查之前。

产物存储与 bridge 继续验证文件完整性；pin 合同不代替这些检查。它也不证明 Harbor Python 环境或任意主机文件的完整依赖闭包。远端 provider 尚不支持此准入合同。

验证入口：`test/evaluation-artifact-pins.test.ts` 检查 CLI/恢复、错误输入、准备阶段拒绝与真实 backend 启动拦截；`test/evaluation-binding.test.ts` 检查持久恢复和幂等冲突；`test/evals.test.ts` 检查两个平台共同执行时保留 pins。调用方 Gear 的安装包旅程另用真实 DSH/Pi 缓存和 Luna 验证。
