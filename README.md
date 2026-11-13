# survey-calibration

企业技术应用调查保存抽样基础权重、响应标志和分层属性，统计响应样本的加权采用率。

除原有的基础权重汇总外，`calibrate` 命令按调查期做非响应校准：用本地目标边际调整权重，并把拟合状态、边际残差、极端权重和有效样本量交给研究人员审查。校准不会自动把缺失结果填成“未采用”，也不会在小样本上伪装精确匹配。

## 使用

运行需要 Python 3.11 或更新版本，没有第三方运行依赖。数据在本地处理，不请求外部接口。

```bash
python3 -m analysis_app summary
python3 -m analysis_app summary --input data/observations.csv --output output/summary.json

python3 -m analysis_app calibrate \
  --targets data/targets.json \
  --internal-output output/internal \
  --release-output output/release
```

标准输出是完整 JSON，输出文件使用临时文件原子替换；失败退出码为 2，不覆盖既有结果。空数据集保留零计数与空分组，缺失数值不进入数值分母。数据指纹由按 observation_id 排序后的解析记录计算。

### calibrate 的输出

`calibrate` 一次写出两个彼此隔离的目录：

- `--internal-output`（默认 `output/internal`）：内部诊断。包含每次校准的逐层边际目标、估计值、绝对与相对残差、权重分布（最小/分位/最大、最大最小比、变异系数、Kish 有效样本量、触及上下限的案例数）、无法由响应样本代表的层，以及被发布抑制隐藏的细分值。目录权限 `0700`，文件 `0600`。
- `--release-output`（默认 `output/release`）：可发布结果。只含总估计、校准状态、区间、按 `size_class` 细分和按 `region` 聚合的可发布单元，不含权重和内部残差。目录权限 `0750`，文件 `0640`。两个目录不能相同或互相嵌套。

标准输出默认打印内部诊断；加 `--release-stdout` 改为打印发布视图。`--output` 可额外保存一份发布结果副本。

### 目标边际文件

`data/targets.json` 是本地示例，按调查期给出总体边际，每期用自己的 `target_version`：

```json
{
  "waves": {
    "0": {
      "target_version": "pop-2026-w0",
      "margins": {
        "region": {"R0": 260.0, "R1": 175.0, "R2": 137.0},
        "size_class": {"large": 300.0, "small": 272.0},
        "industry": {"I0": 145.0, "I1": 145.0, "I2": 127.0, "I3": 155.0}
      }
    }
  }
}
```

三个维度的边际总量必须一致；示例数值是合成的，不对应任何现实总体。文件只接受 `region`、`size_class`、`industry` 三个边际维度，响应记录中出现的每个层级都必须能在同期的目标中找到。

### 校准与区间

- 只使用 `responded=1` 的记录参与校准和估计。`responded=0` 的记录 `adoption` 必须为空，空值不会被当成 0。
- 迭代比例拟合（IPF）在可配置的权重上下限内运行。上下限可能让边际无法同时满足：此时报告 `not_converged` 和真实的最近残差，保留最接近的权重，不归一化成精确匹配。基础权重为 0 的案例始终保持 0。
- 区间由分层自助复制产生：每次复制重新抽取整份观测样本、保留其中的响应者，并**重新校准**后计算采用率，再取百分位区间。并非固定最终权重后套普通比例公式。
- 按 `wave` 比较时，各期用自己的响应记录、目标版本和校准结果。

### 小样本抑制

发布前按 `suppression.minimum_respondents`（默认 5）和 `minimum_rows`（默认 10）检查每个单元。默认开启互补抑制：如果某个划分里恰好只有一个单元被隐藏，再撤销一个已合格单元，使至少两个单元不可见，避免读者用总量相减反推出被抑制单元。某个划分全部不合格时，该期该划分不发布任何单元。发布视图不列出被抑制单元的名称或人数。

## 配置

`--config` 接受 JSON 对象，覆盖默认值：

```json
{
  "calibration": {"max_iterations": 200, "tolerance": 1e-8, "lower_bound": 0.0, "upper_bound": 10.0},
  "bootstrap": {"replicates": 200, "confidence": 0.95, "seed": 20240601},
  "suppression": {"minimum_respondents": 5, "minimum_rows": 10, "complementary": true}
}
```

## 验证与容器

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q analysis_app
docker build -t survey-calibration .
docker run --rm survey-calibration
```

容器内也可执行 `python -m unittest discover -s tests -v`。镜像构建阶段取得 Python 镜像，产品启动、测试和演示均无需下载。小规模样例的完整单元测试、summary 和 calibrate 可以连续执行，不需要等待定时任务。

## 代码与数据

- `analysis_app/io.py`：有类型的 CSV 读取、完整性校验及内容指纹。
- `analysis_app/schema.py`：字段类型、主体与聚合维度。
- `analysis_app/domain.py`：原有基础权重核算函数。
- `analysis_app/summary.py`：确定性的分组与数值口径。
- `analysis_app/calibration.py`：目标边际读取与校验、带上下限的 IPF、权重诊断、每次复制重校准的分层自助区间。
- `analysis_app/analysis.py`：按调查期组织校准、稀疏层报告与内部分析结构。
- `analysis_app/release.py`：小样本抑制、互补抑制与发布视图。
- `analysis_app/output.py`：原子写入与双目录权限隔离。
- `analysis_app/cli.py`：本地命令。
- `docs/data.md`：数据契约。

演示数据与目标边际全部为本地合成，不对应现实机构、设备或人员；不能用来推断真实业务效果。
