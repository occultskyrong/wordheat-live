# 模型质量评测

`20261026-quality-evaluation.py` 使用标准库执行双人标注冻结与真实 API 评分导出汇总：

```text
python 20261026-quality-evaluation.py freeze annotations.json frozen.json
python 20261026-quality-evaluation.py report frozen.json api-scores.json report.json
```

输入包含 schema_version=1、stage=direction|qualification、source_ref、联合 manifest（manifest_id、manifest_sha256、wordbank_manifest_id、calibration_id）和 pairs。每对包含 puzzle_version_id、guess、split=calibration|blind、两名不同 reviewer_id 的 annotations[{reviewer_id,rating:hot|warm|cold|exclude}]；不同意见须有明确 adjudication{reviewer_id,rating,notes}。可选 relation 保留多义/反义/生造词分类。相同题目/词对不能跨 calibration 和 blind；冻结文件不包含机器分，不允许覆盖已有文件。

方向试验完整性为至少20题/200词对；资格试验为至少100题/1000词对，均须覆盖独立calibration/blind。少量输入仍可保存，但complete=false，不能当完整质量试验。

api-scores.json须包含相同manifest、annotation_hash、producer，以及与冻结输入一一对应的pairs[{puzzle_version_id,guess,raw_similarity,display_score}]。report重新核冻结hash、有限数值和集合，计算逐split/逐关系/全部成对排序、分组均值、排除数量；blind数字目标为至少90%。工具仅报告candidate_meets_numeric_threshold；gate_authority始终false，不能写入或授权G1-Q。

无真实有许可词对、独立人工标注或对应真实模型评分导出时，不生成假数据或伪造通过。G1-R仍须Windows/OBS共机资源证据。
