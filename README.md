# 跨区域规则中心的授权合成与门店承诺边界

交付条款来自区域监管线、技术线和商业线。每个签发身份、条款和客户凭证均为本地合成标识。

## 输入消息

每个 JSONL 文件是一段独立输入记录。每行包括整数秒点 at、消息名称 kind 与对象 record。at 代表中间件收到消息的时刻，同一秒内按行序提交。业务时间和对应回执引用写在 record 中。

- **authority**　发布 issuer 在 role、site 与 category 中的管辖修订，active 表示是否负责。新 revision 从事件秒点生效，方式由仿真目录信任。
- **clause**　签发 issuer 和唯一 clause 编号的 rev，绑定 site、category、purpose，可以给 battery 及 required_credentials。kind 是 ban、offer、exception。exception 引用 related 并且只能在引用 offer 的范围内覆盖具体列出的商业凭件。until 为有效期上界。
- **notify_stop**　有效的管辖方对具体 clause 和 rev 发出停止兑现通知，含 reason。从该事件秒点起生效。 在本区域监管范围内可以指向其他签发方的条款。
- **completeness**　门店报告 site、category 范围的已核齐状态。complete 代表当前监管、设备和商业三线的适用条款都已经对齐，可选 cursors 记录各自核齐的最后版本。
- **request**　门店 store 提交 id、site、category、purpose、battery、credentials。
- **commit**　门店把一次 request 变成 promise，expires 是期望的最晚有效秒点。承诺窗口还受签发使用的临时条款到期约束。
- **redeem**　指定 store 对 promise 申请交付。store 的 site 为请求中登记站点，具备相同位于该 site 的合成门店处理资格。
- **inspect**　按 promise 或 request 查看依据和进度。

## 示例

- exception-cannot-rescue-stopped-promise.jsonl
- authority-transfer-after-redemption.jsonl

## 读取演练资料

运行环境为 Python 3.11，只使用标准库。在根目录运行以下命令可核对文件格式及读取接口。

    python3 scenario_reader.py examples
    python3 -m unittest discover -v

资料读取器会列出各文件包含的消息和时间范围。演练记录只描述各参与方交给系统的输入。

## 已实现的中间件

核心实现在 `rule_middleware.py`，只使用 Python 标准库：

- 以 `(issuer, site, category, clause, rev)` 区分条款，并保留同一 `clause` 的全部修订。
- `authority` 校验签发机构在该 `site/category/role` 上当前是否 active；失活旧机构的新条款会得到 `clause_rejected`，不会获得授权效力。
- `ban / offer / exception` 分别对应硬禁止、允许条件和商业例外。
- 例外必须通过 `related`、`related_rev` 或 `related: "id#rev"` 明确引用当前有效的 offer；只能移除明确列出且可豁免的商业凭证，不能解除 ban。
- `request` 给出 `allow / hold / needs_info / deny` 判定、缺失凭证/条件、冲突条款、已知依据和可重新申请条件。
- `completeness.complete=false` 时可以查询已知依据，但不能签发新 promise；兑现门店资料未齐时返回 `held`、`consumed=false`，不会提前消费承诺。
- `commit` 重新评估当前规则，固定精确条款版本，并把承诺窗口缩短为请求 `expires` 与所用 offer/临时 exception `until` 的最小值。
- `notify_stop` 会校验管辖权：原签发方可停止本机构条款；有效监管方可在本区域内停止其他签发方条款。
- 停止通知或窗口到期会阻止未交付承诺兑现；若已经交付，promise 保持 `redeemed`，同时置 `review_required=true` 并记录复核责任门店。
- 跨门店重复兑现返回 `already_redeemed` 和原兑现回执，不会二次交付。
- 一般修订不改变窗口内已签承诺；只有修订中包含 `stop_fulfillment`、`stop_existing`、`stop_promises` 等明确停止兑现标记时，才会形成 breaking change。
- `inspect` 展示签发时使用的条款版本、当前缺失/冲突、时间线、兑现记录以及 `breaking_changes`。

`terminal.py` 提供门店终端和离线结果；`http_service.py` 提供可选的标准库 HTTP 中央服务。所有确认号均以 `SYNTH-` 标识为合成数据，不访问真实机构或凭证服务。

## 运行演练

在根目录运行：

```bash
# 校验原始演练资料
python3 scenario_reader.py examples

# 回放一个或目录内全部 JSONL，输出逐事件结构化回执
python3 main.py examples/exception-cannot-rescue-stopped-promise.jsonl
cat examples/exception-cannot-rescue-stopped-promise.jsonl | python3 main.py --stdin
python3 main.py examples --pretty

# 只看承诺状态摘要
python3 main.py examples --summary

# 启动合成中央服务
python3 main.py --serve 127.0.0.1:8765
# POST http://127.0.0.1:8765/events，GET /inspect?promise=p1
```

测试：

```bash
python3 -m unittest discover -v
```

## Python API 示例

```python
from rule_middleware import RuleMiddleware
from terminal import StoreTerminal, RemoteCentralClient

center = RuleMiddleware()
center.publish_authority("reg", "regulator", "mine", "H", revision=1)
center.publish_authority("tech", "equipment", "mine", "H", revision=1)
center.report_completeness("s1", "mine", "H", True)

center.publish_clause({
    "issuer": "tech", "clause": "fit", "rev": 1, "kind": "offer",
    "site": "mine", "category": "H", "purpose": "fix",
    "battery": "pack", "required_credentials": ["agreement"], "until": 30,
}, at=1)

decision = center.submit_request({
    "store": "s1", "id": "r1", "site": "mine", "category": "H",
    "purpose": "fix", "battery": "pack", "credentials": ["agreement"],
}, at=2)["evaluation"]

if decision["can_commit"]:
    issued = center.issue({
        "store": "s1", "request": "r1", "promise": "p1", "expires": 20,
    }, at=3)
    print(issued["valid_until"], issued["basis"])

print(center.redeem({"store": "s2", "promise": "p1", "site": "mine"}, at=4))
print(center.inspect("p1"))

# 连不上中央服务时，不允许签发或兑现：
terminal = StoreTerminal(
    client=RemoteCentralClient("http://127.0.0.1:9/unreachable"),
    online=True,
)
print(terminal.redeem({"store": "s1", "promise": "p1", "site": "mine"}, at=5))
# status == "offline", can_redeem is False, consumed is False
```

主要状态约定：

| 场景 | `status` | `decision` / `verdict` | 承诺是否消费 |
| --- | --- | --- | --- |
| 可以签发/兑现 | `allowed` / `redeemed` | `allow` | 兑现时消费 |
| 缺凭证或条件 | `missing_information` | `needs_info` | 否 |
| 资料/管辖未齐 | `incomplete` / `held` | `hold` | 否 |
| 硬禁止、停止、到期 | `banned` / `stopped` / `expired` | `deny` / `reject` | 否 |
| 重复兑现 | `already_redeemed` | `duplicate` | 否 |
| 中央不可达 | `offline` | `unavailable` | 否 |
| 交付后收到停止通知 | 仍为 `redeemed` + `review_required` | 复核 | 已交付事实不变 |

