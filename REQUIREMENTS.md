# JavaEE 2026 自动评分需求

## 目标

将各实验抓取与评分数据统一写入本机 CouchDB 数据库 `javaee-2026`，并把评分脚本拆成“公共能力”和“实验规则”两层，便于后续实验复用。

默认 CouchDB UI 地址：

```text
http://127.0.0.1:5984/_utils/#database/javaee-2026/
```

脚本会解析为实际数据库接口：

```text
http://127.0.0.1:5984/javaee-2026
```

## 凭据要求

必须通过环境变量提供账号密码：

```bash
export COUCHDBUSER=...
export COUCHDBPASSWORD=...
```

如果检测不到 `COUCHDBUSER` 或 `COUCHDBPASSWORD`，脚本必须报警并退出，不继续扫描、不写 JSON、不连接数据库。

## 强客观证据

强客观证据由脚本直接给分，限于可稳定、可重复检测的数据：

- Maven 构建成功标志：`build.build_ok is True`
- `pom.xml` 是否存在
- 分支提交次数
- distinct authors 作者数
- 文档文件是否存在，包括 step 文档和 AI 日志

这些证据写入每个组的 `auto_scores`。

实验 1 的强客观上限必须按指导书评分项映射，不使用假设值：

| 评分项 | 总分 | 强客观上限 | 模型主观上限 |
| --- | ---: | ---: | ---: |
| 开发环境配置与验证 | 15 | 3 | 12 |
| 骨架项目创建与导入 | 15 | 3 | 12 |
| 国内镜像源配置与构建成功 | 15 | 6 | 9 |
| 基础运行与页面标识修改 | 15 | 0 | 15 |
| 过程记录文档 | 15 | 3 | 12 |
| AI 使用记录 | 10 | 1 | 9 |
| 多次提交与渐进完成 | 5 | 4 | 1 |
| 组内成员提交完整性 | 10 | 6 | 4 |

合计：强客观 26 分，模型主观 74 分。模型主观上限固定为每项 `subjective_max`，不得按 `总分 - 实际客观得分` 动态放大。缺失的强客观分不得由模型主观分补回。

## 弱客观与主观证据

弱客观证据不直接给分，只写入 `ai_pending.materials` 供模型评分：

- JDK/Git/Maven 版本文本命中
- Spring Boot 结构、`src/main/java`、`Application` 类、`groupId`
- 镜像源配置线索
- 页面模板、静态页面、Controller 返回页面、页面标识命中
- 文档字数、章节覆盖、问题处理记录
- AI 日志 hash 关联
- 提交跨天数、语义化率、提交粒度
- 成员提交实质性、贡献均衡度

## 图片证据

如果文档目录包含图片，脚本必须导出图片，以便模型评分时读取。

导出位置：

```text
grading-1/figures/<组名>/
```

支持：

- Markdown 图片：`![...](...)`
- HTML 图片：`<img src="...">`
- `docs/` 下未被引用但实际存在的图片文件
- base64 data image

每个模型评分材料里保留 `doc_figures`，包含原始引用、来源文档、导出路径。

## 脱敏要求

输出 JSON 与 CouchDB 文档中不得保留花名册中的真实姓名和学号。

入口脚本必须显式提供 `--roster`；不得使用默认花名册路径。文件不存在或解析为空时应报警退出。

脱敏占位规则：

- 姓名：`STUDENT_001`
- 学号：`ID_001`
- 邮箱：`EMAIL_001`
- Gitee 用户：`GITEE_001`

脚本写出 JSON 前执行脱敏自检；发现花名册原值时必须报警。

## CouchDB 存储要求

每次扫描/评分后写入三类文档：

- 每组结果文档：`autograde:<experiment_id>:group:<branch>`
- 最新索引文档：`autograde:<experiment_id>:latest`
- 本次运行文档：`autograde:<experiment_id>:run:<timestamp>`

每组文档必须保留：

- `branch`
- `last_commit_hash`
- `build_success`
- 完整 `item` 评分材料

下一次扫描时，如果某组 `last_commit_hash` 与远程分支 tip 一致，且上次 `build_success` 为 `true`，可以跳过重新评分，直接复用 CouchDB 中的旧结果。若 hash 变化或上次构建失败，应重新评分。
