# Insightful Report Agent

基于 Design Agent 的 Deep Agents 自托管 harness，通过 CLI、飞书、Slack 和 A2A 收集材料，交付可离线打开的 HTML 报告。报告可包含表格、原始图片、图表和可点击的引用。每条引用指向报告内的证据摘录，并标出原始文件、页码、行号或视频时间戳。

## 启动

```bash
uv sync --extra feishu --extra slack
# 首次配置时，从 .env.example 创建本地 .env，并填入 DEEPSEEK_API_KEY。
uv run insightful-report-agent sandbox build
uv run insightful-report-agent doctor
```

默认主模型为 `openai:deepseek-flash`。先将原始文件（也可以是 ZIP）、网页或文件夹提交给部署方提供的 **Bulk Reference Files Preprocessor**，再把它生成的结果 ZIP 和报告目标交给报告 Agent。可以设置 `INSIGHTFUL_REPORT_AGENT_PREPROCESSOR_DOCS_URL`，让 Agent 告知用户在哪里提交原始材料。

设置预处理服务的 API 地址和密钥后，各入口也可直接接收原始附件。可在 Langfuse 中查看从接收到交付的流程和模型调用。配置见 [运行与接入](docs/operations.md#langfuse-运行追踪)。

```bash
uv run insightful-report-agent ask --thread cli-research --file ./preprocessed.zip \
  '请基于所有材料撰写竞品分析，解释主要分歧与证据缺口，并用图表呈现可比数据。'
```

同一 thread 可以分批上传结果 ZIP。Agent 直接使用索引、简报、正文与图片核对证据，逐章保存长报告草稿与写作进度。交付文件在 `workspace/out/<thread>/`。各入口的 thread 独立保存。

| 入口 | 启动和用法 |
| --- | --- |
| 飞书 | `uv run insightful-report-agent feishu`。私聊或在同一群话题上传预处理结果 ZIP。上传完成后发送报告目标。 |
| Slack | `uv run insightful-report-agent slack`。使用同一线程，群里上传预处理结果 ZIP 和提交报告请求时 @机器人。 |
| A2A 与 HTTP | `uv run insightful-report-agent serve`，也可在 VS Code 按 F5。两个 API 使用同一端口，飞书和 Slack 随配置接入。通过 Text 提交报告目标，通过 Raw 或 multipart 上传预处理结果 ZIP。 |

接入权限、批量接口和容量限制见 [运行与接入](docs/operations.md)。摘要、材料归一化和引用见 [来源工作流](docs/source-workflow.md)。

## 验证

```bash
uv run ruff check .
uv run pytest -q
INSIGHTFUL_DOCKER_TESTS=1 uv run pytest -q
```
