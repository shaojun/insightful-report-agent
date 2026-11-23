# 运行与接入

配置从本地 `.env` 或进程环境读取。长期任务可由服务机上的常驻进程接收。各 thread 使用独立沙箱。默认保留已停止的 workspace，可通过 `INSIGHTFUL_REPORT_AGENT_SANDBOX_RETENTION_SECONDS` 设置自动回收时间。SQLite 保存对话和任务状态，也可安装 `--extra self-host` 后配置 PostgreSQL。

## 飞书

填入 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 和 `FEISHU_TENANT_KEY`。在飞书开放平台启用机器人，选长连接接收事件并订阅 `im.message.receive_v1`，发布包含所需权限的应用版本。参考 [权限配置样例](../config/feishu-permissions.example.json)。

运行 `uv run insightful-report-agent feishu`，或用 VS Code 选择 Feishu 启动配置。只启动一个消费者。群内首次 @机器人建立话题，后续材料和报告目标留在同一话题。飞书入口每条最多处理 10 个附件，并受平台文件大小限制。上传预处理服务生成的结果 ZIP。私聊默认持续收集，发 `/new` 开始新会话。

上传后，飞书会回报结果包内的可用来源和错误。用户可以继续上传或发送报告目标。报告会等待来源目录准备完成。

## Slack

使用 [Slack manifest 样例](../config/slack-manifest.example.yaml) 创建应用，安装到工作区，配置 `SLACK_BOT_TOKEN` 和 `SLACK_APP_TOKEN`，运行 `uv run insightful-report-agent slack`。Socket Mode 是默认值。群内在同一个 thread 附带 @机器人上传一个或多个预处理结果 ZIP，最后发送报告目标。每条最多处理 10 个文件。切换到签名 HTTP 时设置 `SLACK_TRANSPORT=http` 和 `SLACK_SIGNING_SECRET`。

## A2A 1.0 和 HTTP

设置 `INSIGHTFUL_REPORT_AGENT_A2A_URL`（例如 `http://127.0.0.1:8787`）和 `INSIGHTFUL_REPORT_AGENT_ACCESS_TOKEN_CALLERS`。后者接受逗号分隔的 `token:caller`。A2A 默认需要 Bearer 凭证，按调用者与 `contextId` 隔离工作区。Agent Card 在 `/.well-known/agent-card.json`，JSON-RPC 在 `/a2a`。可选的 Nacos 注册通过 `INSIGHTFUL_REPORT_AGENT_NACOS_*` 配置。

首次 `SendMessage` 可附带 Raw 结果 ZIP 或发送短消息创建 context，后续使用响应的 `contextId`。Raw 每个文件最多 16 MiB，每条最多 16 个且总计 32 MiB。网页作为参考材料时，应先提交给预处理服务。

```bash
curl -fsS -H "Authorization: Bearer $REPORT_TOKEN" \
  -F "files=@preprocessed.zip" \
  "$REPORT_BASE/a2a/contexts/$CONTEXT_ID/attachments"
```

multipart 可重复上传，每次最多 100 个文件、单文件 200 MiB、合计 256 MiB。导入时每个结果 ZIP 最多包含 25,000 个文件、8 GiB 展开内容和 200 MiB 单个成员，实际接入仍受各渠道的上传大小限制。报告通过 A2A Artifact 返回，下载使用同一 Bearer 凭证。`SendStreamingMessage` 可订阅任务状态和工具进度。独立 HTTP 客户端可以用 `POST /threads/api-my-thread/attachments` 上传，再用 `POST /threads/api-my-thread/messages` 提交 `{"text":"…"}`。HTTP thread 与 A2A context 各自隔离。

## Nacos 注册

A2A 端点启动后，注册器在 Nacos 查询或发布 Agent Card，再注册当前实例的 A2A endpoint 并发送心跳。实例正常退出时注销自己的 endpoint。Agent Card 已发布后，如内容有变化，递增 `INSIGHTFUL_REPORT_AGENT_A2A_VERSION`。Nacos 需要 3.3 Agent Registry API，应用账号需要相应命名空间和 Agent 的定义发布及 endpoint 写权限。 [Nacos Agent Registry](https://nacos.io/en/docs/latest/manual/user/ai/agent-registry/)

配置前可看 [带注释的环境变量示例](../.env.example)。登记时设置两个 origin。 `INSIGHTFUL_REPORT_AGENT_NACOS_URL` 是 Nacos 地址，`INSIGHTFUL_REPORT_AGENT_A2A_URL` 是调用方能直接访问的本 agent 地址。若服务通过反向代理对外，A2A URL 写代理对外的 origin，API bind 地址按部署环境配置。A2A Bearer token 与 Nacos 应用账号独立。

可用单独的服务配置文件覆盖项目 `.env`。启动后先检查本实例的 `/health` 是否返回 `nacos.registered=true`，再从调用方验证 Agent Card 和 A2A 请求。

```bash
uv run insightful-report-agent serve --env-file .env.a2a
```
