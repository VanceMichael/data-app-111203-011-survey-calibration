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
| `adoption` | float | 允许空值，空值不是零 |
| `responded` | int | 必填 |
| `base_weight` | float | 必填 |
| `wave` | int | 必填 |

时间字段 day、week、tick、hour、minute、cycle、start_day、reported_day、known_at、visible_at、opened、observed 均以各字段名所述的相对时间单位表示整数；不同字段并不必然代表同一种时间。缺少的登记码与浓度等信息保持为空。示例只表达产品数据结构，不能代替实际调查资料。

## 业务字段语义

adoption 是响应企业的二元采用结果；responded=0 时必须保留 adoption 为空，不代表不采用。base_weight 是基础抽样权重。wave 为调查期，region、size_class、industry 为总体边际维度。
