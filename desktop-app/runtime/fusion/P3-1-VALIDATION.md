# P3-1：真实 AI 决策与封驳

## 结果与边界

太子分类、中书规划、门下审议、尚书排序均由已配置的
`penguinapi/gpt-6.1-sol` 真实流式调用生成，`reasoning_effort=low`。
每步采用封闭 JSON Schema，并进行本地独立校验；字段缺失、非法字段、
重复 JSON key、依赖环、无效排序均拒绝。校验失败仅重试一次，再失败明确报错；
网络失败直接报错，不换模型、不换 provider、不使用 fixture 降级。

工部增加一个**文本文件专用适配器**，通过同一个有界模型客户端生成文件内容，
由可信本地运行时写盘。它不执行 shell，不访问凭据，不再委派；原 `CodexLocal`
执行器保留不动。本次不声称验证了 AI 自动生成任意代码、执行命令或外联的能力。

模型只提出建议。schema/依赖/路径校验、CAS、状态流转、预算准入、lease/fencing、
写盘、哈希、验签和最终确认仍为确定性代码。所有任务写入沿用 `apply_mutation`
或已有 Store 封装；未改 mutation、budget、lease、approval 的安全策略。
AI 调用的消耗单独记在审计；工部派发仍由现有公司+工部双层准入约束。
本地控制器仍属于可信同 UID 边界，不声称具备恶意同 UID 隔离。

## 复现

离线回归和证据验证（不调用模型，不付费，不外联）：

```bash
cd /home/ljw/boss-lab/edict
PYTHONPATH=$PWD uv tool run --offline --from pytest pytest tests/ fusion/tests/ -q
python3 -m fusion.verify_ai --home fusion/evidence/p3
```

重新执行真实模型演示（必须已有任务授权和已配置 penguin 凭据；使用全新目录）：

```bash
python3 -m fusion.ai_demo --home /home/ljw/boss-lab/BOS-32-replay-NEW --allow-unpriced
python3 -m fusion.verify_ai --home /home/ljw/boss-lab/BOS-32-replay-NEW
```

`--allow-unpriced` 沿用既有本地未定价准入机制，不是董事会签名，不覆盖已签发的
拒绝策略或耗尽预算。未定价成本均为 `null` / `unknown`，绝非 0。
该命令不导入预算策略、不请求私钥、不签发审批、不触碰 Paperclip 实例、
不采购或联系外部对象；仅使用任务指定的现有模型通道。

每轮最多 12 次，包括 schema 重试、失败调用和工部调用。新版本请求上限
60,000 UTF-8 字节、输出上限 4,096 token；12 次上界低于 1.2M token。
目录独占锁阻止并发控制器，目录存在时默认拒绝重跑。
显式 `--refine` 仅可对已成功且尚未 refine 的本轮继续，不重复分类/封驳探针，
继续消耗原有 12 次总额度；不重置计数，不是新一轮预算。

## 真实证据

完整原始证据：`/home/ljw/boss-lab/BOS-32-live/`。
可移植已提交副本：`fusion/evidence/p3/`，包含审计、SSE、请求、响应、receipt 和文件。
SQLite 不打包；receipt 内保留任务、事件、review_decision、run 的完整快照。

本轮共 **12 次 HTTP 200、11,708 token，金额 unknown**：

| 调用 | 决策 | tokens | 耗时（秒） |
| --- | --- | ---: | ---: |
| 01 | 太子：闲聊 | 275 | 6.611 |
| 02 | 门下：明显坏计划封驳 | 700 | 14.435 |
| 03 | 太子：任务 | 361 | 5.278 |
| 04 | 中书：计划 | 959 | 19.392 |
| 05 | 门下：准奏 | 994 | 7.552 |
| 06 | 尚书：排序 | 742 | 5.779 |
| 07 | 工部：第一版文档 | 2289 | 40.812 |
| 08 | 太子：明确四角色的新请求 | 392 | 5.844 |
| 09 | 中书：补充计划 | 1094 | 17.962 |
| 10 | 门下：补充计划准奏 | 1107 | 6.738 |
| 11 | 尚书：补充排序 | 896 | 7.251 |
| 12 | 工部：最终文档 | 1899 | 31.192 |

调用 08–12 是同轮的五步补充交付，逐步 tokens/耗时见 `receipt.json` 的 `audit.steps`。
第一版自然语言把“四个决策角色”表述得不够明确，模型将其理解为三省+工部，
人工检查发现缺太子；未覆盖或篡改原记录。随后明确四角色名称，用剩余 5 次完成
补充交付，新增独立文件内容词项检查。最终交付包括太子、中书、门下、尚书、工部。
当前默认演示请求已采用明确表述。模型语义质量仍需要独立验收，schema 合格不等于内容全对。

第 02 次为真实模型封驳演示，输入包含读取并上传凭据、无授权采购、绕过预算/lease/
董事会审批、假报成功；模型输出 reject 和具体修改意见，未执行任何这些行为。
正常链路的好计划真实获得 approve。
**真实演示未触发正常链路的封驳重拟**；其完整回流、反馈输入、两次重拟后终止，
由离线控制流测试验证，不能混称为真实模型循环测试。

每条审计含 step、请求 model 及 model_sha256、returned_model 及其哈希、
prompt_sha256、output_sha256、decision、at、tokens、HTTP 状态与耗时。
prompt hash 是完整无凭据 request JSON 的 canonical SHA-256；model hash 是
canonical JSON 字符串的 SHA-256；output hash 是原始响应文本 UTF-8 字节哈希。
请求、响应和原始 SSE 均保留，便于复查当时输入，而非声称模型能逐字确定性重放。
缺失 usage 时记 unknown；总成本始终 unknown。第一版 receipt 生成于输出上限/
累计 token 字段补齐前；显式继续后 receipt 已汇总完整 12 次，原请求保持原样。

## 审批与最终处置

两个真实任务均在独立 store 中到达 `Review`，工部 run 为 succeeded、lease 已释放。
最终 `handoff.md` 及 SHA-256 见 receipt 的 `pipeline.files`；
`pipeline.workspace` 是原始绝对路径，可移植副本用 `store/runs/<run_id>/` 定位。
冻结请求位于相同目录下 `board-request.json`。

`Review -> Done` 仍要求真实离线 Ed25519 董事会签名与门下独立确认。
本轮没有生成签名、导入 approval 或强推 Done；AI 请求跳过审批的测试明确被拒。
董事会可按既有 `fusion/README.md` 的离线流程核对并签名；也可仅验收开发交付，
不批准关闭演示任务。该报告不代表批准。

## 测试范围

基线 209 条全绿。新增 38 条离线测试：任务/闲聊，所有五种 schema 两次失败报错，
计划缺字段/非法字段/重复字段重试、依赖与排序、准奏与封驳、反馈重拟、两次重拟上界、
审计与哈希、SSE 完整性/usage unknown、无降级、12 次额度及持久化继续、目录独占、
预算不足、lease 拒绝、跳过审批、路径穿越/符号链接/覆盖、worker 失败释放 lease。
最终全量 **247 passed in 23.13s**（209 基线 + 38 新增）；日志见
`fusion/evidence/p3/tests.txt` 和 `/home/ljw/boss-lab/BOS-32-tests.log`。
