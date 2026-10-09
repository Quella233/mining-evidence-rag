# 数据说明

## 截止日期与验收口径

本次采集以 `2026-10-08` 为截止日，近30天指 UTC 日历日
`2026-09-09T00:00:00Z <= published_at < 2026-10-09T00:00:00Z`。
价格按交易日、品种、合约、价格口径统计。文章按原文统计，切块不增加文章条数。
周末和休市日没有行情时不填充、不复制、不补造价格。

最终数量以 `data/coverage-report.json` 为准。测试通过与真实数据验收是两件事：
HTTP 403、授权缺失、政策发布频率不足，都不能用合成数据补成“每源200条”。
题目可能把“覆盖近30天”和“近30天至少200条政策”混为一谈，必须和面试方确认。

## 数据源

| 来源 | 采集方式 | 说明 |
| --- | --- | --- |
| mining.com | RSS 分页发现链接，再获取全文 | 初次访问403；记录失败，不绕过访问控制 |
| S&P Global Mining | 可配置的同一RSS适配器 | 截图没有实际RSS链接，未猜测私有接口 |
| 澳洲 DISR | 官网新闻与关键矿产战略页、同站链接发现 | Drupal发布日期字段，JSON-LD/元标签兜底；不把更新时间当发布日期 |
| 中国稀土集团 | 官网同站公开页面 | 站点可达性、结构和日期字段需要真实响应验证；不能访问时记错误 |
| LME 铜锌镍 | 授权导出导入入口 | 页面访问403；没有提供许可数据，不宣称完成 |
| 上海钢联铁矿石 | 授权 CSV / JSONL 导入 | 未配置账号，无模拟报价 |
| 上期所铜锌镍 | 公开每日JSON行情 | **公开补充来源，不是 LME**；人民币/吨、期货结算价、独立合约 |
| International Mining / Mining Technology | RSS + 全文 | **公开补充新闻，不冒充原题指定媒体** |

截图的“SHFE 锂”需澄清：国内碳酸锂期货在广期所 GFEX。不能把上期所铜锌镍
价格当成锂价，也不能把交易所期货价与钢联现货报价混用。

## Schema

`documents` 存原始文档，`chunks` 存分块文本，`vectors` 是 sqlite-vec 的384维
向量虚表。SQLite WAL 与事务保证更新正文、删除旧块、重建向量同时完成。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| id | string | 确定性24位 SHA-256 标识 |
| kind | news / policy / price | 三类数据 |
| source | string | 实际来源名称 |
| url | HTTP(S) URL | 可追溯原文/行情地址 |
| title / body | string | 清洗后标题和全文；不能为空 |
| published_at | UTC datetime | 可验证的发布时间/交易日收盘时间，不使用采集时间代替 |
| retrieved_at | UTC datetime | 入库采集时间 |
| country / commodity | nullable string | 国家、矿种；词典辅助提取，存在多主题文章误判的局限 |
| effective_date | nullable date | 政策生效日期，未可靠解析时保持空值，不等同发布日期 |
| price | positive finite float | 报价；拒绝 NaN / Infinity |
| currency / unit | nullable string | 币种与计价单位 |
| contract / price_type | nullable string | 合约与口径，防止错配报价 |
| is_demo | boolean | 合成数据标识，正式API默认排除 |

上期所日期指交易日，固定以07:00 UTC表达收盘时间。仅有日历日的文章日期用
当日00:00 UTC归一化，不伪造精确发布时间。带时区的原始时间转换为UTC。

## 主键与去重

文章主键：规范化 URL 的 SHA-256 前24位。只清理 fragment、utm_*、fbclid、gclid，
保留有意义的查询参数，排序参数，不合并HTTP和HTTPS地址。

价格主键：规范化 URL + 来源 + 日期 + 矿种 + 合约 + 口径 + 币种 + 单位。
同一天同品种不同合约各是一个真实观察值，不是重复的同一条报价。

文章增加 `kind + cleaned_body` 的全文哈希检测同文转载；正文改变时按稳定主键更新。
不做可能误合并政策修订的模糊去重。正文每1000字符切块、重叠150字符。
检索返回按原始文档去重后的 top-k，不让同一文章多个块占据前五。

## 检索与回答

正式库 `data/mining-semantic.db` 使用本地 FastEmbed
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，支持中英文。
向量余弦排序与 BM25 通过 RRF（常数60）融合。先按国家、矿种、类型和时间过滤。
当前采用精确向量扫描，适合面试数据量；十万块以上应改分区ANN/专用向量服务。

`hash` 是无需模型的自动测试/采集基线，使用确定性词哈希，**不是语义模型**。
模型库和哈希库必须分开；程序拒绝不同向量空间混用。

`/query` 为原文摘录型回答，返回可点击来源、发布日期与逐条引文。没有外部LLM、
没有API Key依赖，也不会把资料中的指令当成系统指令执行。英文资料可用中文检索，
但引文保留英文；跨文档总结、翻译和价格趋势计算不是当前实现的功能。
问“有何变化”时列举相关证据，不在覆盖不全时断言“没有变化”。

## 采集与错误处理

公开文章遵守robots.txt；robots读取失败或拒绝时停止对应来源。公开JSON行情属于
独立API适配器。每域限速，超时与429/5xx有限重试，遵守有上限的Retry-After。
403、登录墙、正文过短、缺失发布日期都记录，不绕过、不伪装成成功。

`data/cache/http` 保存成功原始响应、请求URL、时间、状态和SHA-256，便于审计和
离线重新提取；这是证据归档，不会把旧缓存无提示当成最新网络结果。
爬虫的URL来自本地配置，不允许通过 `/query` 输入任意URL发起抓取。

JSONL/CSV导入使用同一Schema，每条验证后入库，错误标明行号。逐条提交支持幂等
续跑；一个批次的后半部分失败时，前面成功记录仍保留，不承诺批次全回滚。

## 评测边界

`eval/gold.jsonl` 是20条人工编写的**合成测试问答**，只写入 `data/demo.db`。
多语言模型同一题集另存在 `data/demo-semantic.db`，不进入正式数据统计。

Recall@5 = 前5个原始文档命中相关文档数 / 人工标注相关文档总数。
Answer faithfulness = 回答中有正确引用且原文逐字支持的摘录数 / 回答摘录数。
这是适用于extractive回答的引用一致性指标，不冒充独立模型的语义忠实度评分。
拒答没有可评分摘录，分数记null而不是自动100%；另报 answered_cases。
还检查 expected_answer_accuracy，防止“引用真实但没答中问题”。

真实领域评测应由业务人员对实际抓取文档标注20条Q&A，明确 document_id、标准答案、
时间、类型等。运行 `eval.run --db data/mining-semantic.db --embedding fastembed
--gold eval/real-gold.jsonl`。不提供 `--allow-demo`，防止误把合成成绩当真实成绩。

本次已附 `eval/real-gold.jsonl`：10条实际抓取新闻、10条上期所当日合约行情的人工
核对关键事实，`eval/real-report.json`保存成绩。部分问题保留英文专有名词；不声称
这是纯中文跨语言难题集。没有真实政策样本，因此真实政策效果尚未通过此评测验证。
