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
