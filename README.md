# Autograde

实验 1 入口：

```bash
export COUCHDBUSER=...
export COUCHDBPASSWORD=...

python3 -m autograde.run_experiment1 \
  --repo-dir project-1/grading-work/experiment-1 \
  --roster rosters.txt \
  --output project-1/grading-experiment1.json \
  --figures-dir grading-1/figures
```

默认连接：

```text
http://127.0.0.1:5984/_utils/#database/javaee-2026/
```

如果环境变量缺失或 CouchDB 无法连接，脚本会报警退出。

`--roster` 必须显式提供；文件不存在或解析为空时脚本会报警退出。

实验 1 按指导书 8 个评分项拆分为强客观 26 分、模型主观 74 分：

- 开发环境配置与验证：3 + 12
- 骨架项目创建与导入：3 + 12
- 国内镜像源配置与构建成功：6 + 9
- 基础运行与页面标识修改：0 + 15
- 过程记录文档：3 + 12
- AI 使用记录：1 + 9
- 多次提交与渐进完成：4 + 1
- 组内成员提交完整性：6 + 4
