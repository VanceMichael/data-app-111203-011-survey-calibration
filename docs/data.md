# 数据契约

企业技术应用调查保存抽样基础权重、响应标志和分层属性，统计响应样本的加权采用率。

主测量 `adoption` 的单位为二元值，主体字段是 `firm`，核算维度是 `region`。`observation_id` 是文件内唯一观测编号，主体允许重复观测。数值只接受有限值；空单元格解析为 None，不能直接当成零。额外列不进入当前核算。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `observation_id` | str | 必填 |
| `firm` | str | 必填 |
| `region` | str | 必填 |
| `size_class` | str | 必填 |
| `industry` | str | 必填 |
| `adoption` | float | 响应者必填且为 0 或 1；未响应者必须为空 |
| `responded` | int | 必填，只能为 0 或 1 |
| `base_weight` | float | 必填，有限非负数；0 表示该案例不承载代表性 |
| `wave` | int | 必填 |

时间字段 day、week、tick、hour、minute、cycle、start_day、reported_day、known_at、visible_at、opened、observed 均以各字段名所述的相对时间单位表示整数；不同字段并不必然代表同一种时间。缺少的登记码与浓度等信息保持为空。示例只表达产品数据结构，不能代替实际调查资料。

## 业务字段语义

adoption 是响应企业的二元采用结果；responded=0 时必须保留 adoption 为空，不代表不采用。base_weight 是基础抽样权重。wave 为调查期，region、size_class、industry 为总体边际维度。

同一 firm 在同一 wave 最多有一条记录，跨 wave 可以重复接受调查；同一 wave 内重复出现会被读取阶段拒绝。

## 非响应校准的输入与输出

校准读取本地目标边际文件（示例 `data/targets.json`），结构为 `waves -> 调查期 -> {target_version, margins}`；`margins` 必须恰好包含 `region`、`size_class`、`industry`，三个维度的总量必须相等。每期使用自己的目标版本；响应记录中出现的层级必须在该期目标中定义。

校准只使用 `responded=1` 的记录计算采用率；`responded=0` 的记录只用于说明哪些层没有响应者，绝不贡献 adoption。输出分两份：

- 内部诊断：校准状态、逐层边际残差、权重分布与有效样本量、无响应者的层、被抑制的细分值。
- 发布结果：总估计与区间、可发布细分；不含权重、残差，也不列出被抑制单元的名称或人数。

单元小于阈值时抑制；若某个划分恰好只剩一个待隐藏单元，会再撤销一个已合格单元（互补抑制），避免通过总量相减反推。两处输出使用不同目录与权限，不能嵌套。
