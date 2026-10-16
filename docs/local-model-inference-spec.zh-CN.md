# 普通模型端点接入状态

普通推理已切换为已部署 API 端点。Hitch 不再部署或管理普通推理引擎、GPU 账本、模型节点 RPC 或 Python 推理服务。

当前接口、冻结绑定、工具协议、取消、计量与历史读取说明见[模型推理指南](guide/zh-CN/model-inference.md)。固定版本上游部署与实机验收清单见[部署示例](../examples/external-inference/README.md)。2026-10-15 已完成限定范围的[真实 TP=2 / Gateway 双副本短验收](validation/external-inference-gpu-2026-10-15.md)，另完成[静态训练/推理分卡诊断](validation/training-gpu-loop-2026-10-15.md)：真实 Slime 一次更新、checkpoint/HF 导出及普通 TP=1 端点的 Harbor 基线/candidate 评测配对，均 valid / reward=0、meanDelta=0。后者明确 validated:false、runtime lock 为 pending-gpu、loss/grad norm=0，不构成正式认证或学习提升；完整本机制品全闭包与离线 finite 复核已通过（108 个对象、35,467,670,234 字节），精确租机销毁已确认，Compose 容器、完整 benchmark 与远程 Harbor worker 尚未验收。旧自研推理池证据不能替代新路径验收。

模型 manifest、CAS、历史锁与训练精确采集继续保留。旧服务应使用旧版本工具完成停止与物理释放确认后，再移除旧部署环境。
