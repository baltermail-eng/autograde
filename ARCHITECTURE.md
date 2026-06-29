# JavaEE 2026 自动评分架构

## 分层

```text
autograde/
  common/
    utils.py       # Git、Maven、文档读取、图片导出、脱敏、提交统计
    couchdb.py     # CouchDB URL 解析、凭据检查、文档读写、增量索引
    grading.py     # 通用 auto_scores / ai_pending entry 构造
  experiments/
    experiment1.py # 实验1 rubric、prompt、强客观规则、弱证据材料
  run_experiment1.py
```

公共层不包含具体实验评分标准；实验层只描述本实验 rubric、prompt 和证据映射。

## 数据流

1. 入口脚本读取 CouchDB 环境变量并连接数据库。
2. 获取远程分支列表与每个分支 tip hash。
3. 查询 CouchDB 中每组的历史文档。
4. 若 hash 未变且上次构建成功，复用旧结果。
5. 否则检出分支到临时工作树。
6. 公共层采集强客观数据：
   - 构建结果
   - `pom.xml`
   - 提交次数
   - 作者数
   - 文档文件存在
7. 公共层采集弱证据：
   - 代码结构信号
   - 文档全文
   - 图片导出清单
   - 提交明细
8. 实验层生成 `auto_scores` 与 `ai_pending`。
9. 写出本地 JSON。
10. 写入 CouchDB 每组文档、最新索引文档和运行文档。

评分分道规则：

- `RUBRIC[].max` 是指导书分项总分。
- `RUBRIC[].objective_max` 是强客观证据上限。
- `RUBRIC[].subjective_max` 是模型主观评分上限。
- `auto_scores[].max` 使用 `objective_max`。
- `ai_pending[].max` 使用 `subjective_max`，不随实际客观得分变化。
- 缺失的强客观分记录为 `objective_missing`，不得由模型主观分补回。

实验 1 当前映射为强客观 26 分、模型主观 74 分。

## CouchDB 增量判断

每组文档保存：

```json
{
  "type": "autograde_group_result",
  "experiment_id": "experiment1",
  "branch": "group01",
  "last_commit_hash": "...",
  "build_success": true,
  "item": {}
}
```

跳过条件：

```text
current_tip_hash == last_commit_hash
AND build_success == true
```

设计含义：

- hash 不变且上次构建成功：结果稳定，可复用。
- hash 不变但上次构建失败：允许重试，避免临时环境问题长期卡住。
- hash 变化：必须重新抓取、构建、评分。

## 图片处理

图片由公共层导出，实验层只引用 `doc_figures`。

`doc_figures` 示例：

```json
{
  "original_ref": "img_1.png",
  "source_kind": "markdown_reference",
  "source_path": "docs/img_1.png",
  "source_doc": "docs/step1-git-setup.md",
  "exported_path": "/Users/ben/course/javaee/grading-1/figures/group01/..."
}
```

模型评分时如果 `doc_figures` 非空，应读取 `exported_path` 对应图片，把截图中的环境命令输出、IDE 导入、运行页面和构建结果作为主观证据。

## 后续实验扩展

新增实验只需要：

1. 在 `autograde/experiments/` 下新增 `experimentN.py`。
2. 定义 `EXPERIMENT_ID`、`RUBRIC_ID`、`RUBRIC`、`PROMPTS`。
3. 实现 `score_branch(...)`，调用公共层采集结果。
4. 增加一个 `run_experimentN.py` 或抽象统一入口。

公共层保持稳定，不把某一章的业务规则写入公共模块。
