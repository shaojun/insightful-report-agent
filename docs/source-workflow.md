# 来源工作流

默认入口接收 Bulk Reference Files Preprocessor **生成的结果 ZIP**。原始文件、原始 ZIP、网页和 WebDAV 路径由该服务处理。报告 Agent 导入归档时保留 ZIP 校验值和每份导出正文的校验值，遍历 `wiki-index.md` 及分页索引，按原始材料登记成功与失败。正文和图片保留导出行位置。归档未提供原始二进制文件的校验值或物理页码时，报告将明确使用导出定位。原图在正文取证时按需读取，不触发 OCR 或摘要模型。

工作流通过 `prepare_source_overview` 读取分页覆盖结果，`synthesize_source_overview` 建立当前快照的可发布覆盖目录。随后按章节查阅现有简报与原文，用 `publish_report(validate_only=true)` 渲染检查，再交付 HTML。

## 旧版原始文件收件

旧线程需要原始文件解析时，可在独立实例中设置 `INSIGHTFUL_REPORT_AGENT_SOURCE_INPUT_MODE=legacy`。以下解析、搜索、转写与摘要设置仅用于旧版模式。

## 归一化与定位

| 来源 | 处理方式 |
| --- | --- |
| Markdown、文本、CSV、JSON | 保留内容并记录原始行范围。带相对路径图片的 Markdown 可和图片一起放进 ZIP。 |
| PDF、DOC、DOCX、常见图片，以及 MinerU 支持的 PowerPoint 和 Excel | 配置 `MINERU_API_TOKEN` 后使用 MinerU v4 精准解析。保留结果 ZIP、Markdown、图表和图片。返回页码时记录页码，否则标注提取后的 Markdown 行号。失败可重新执行所在批次。 [MinerU API](https://mineru.net/apiManage/docs) |
| 未启用 MinerU 时的 Word、PDF、图片 | 在 Docker 内使用 LibreOffice、Pandoc、Poppler 和 Tesseract。PDF 提取文本和最多 150 页预览，对有预览的扫描页做 OCR。 |
| 公开 URL | 保存链接、抓取内容和校验值。登录墙、依赖脚本生成的正文和下载失败的图片可能取不到。搜索使用 DeepSeek Search、Exa 或管理员配置的 MCP。搜索结果须登记并成功解析才能引用。 [DeepSeek Search](https://api-docs.deepseek.com/guides/search/) |
| ZIP | 保留包路径和成员路径，逐一解析。最大 750 个成员和 512 MiB 展开体积。加密、内嵌 ZIP、损坏或不安全的条目进入失败记录。 |
| MP4 等视频 | FFmpeg 采样最多 16 帧，保留时间戳、预览和 OCR。配置 `GROQ_API_KEY` 可转写前 3 小时音轨。采样可能错过帧间事件。 |

源文本与视觉资产保存在 thread 的 `/workspace/source-library/`。视觉模型默认按文档选取最多 12 张预览做解释，其他预览仍可按需查看。视觉解读和转写有独立的来源位置。模型、MinerU 或转写服务会收到完成该步骤所需的材料，具体取决于管理员配置。

## 收件与预处理

接受上传后，Host 将原件保存到会话沙箱，并启动后台处理。受支持的文件通过 MinerU 解析，归一化结果写入来源版本目录的 `source.md`，由摘要模型生成 `原文件名.abstract.high`。文件越多时，处理顺序由队列和配置的并发数决定。飞书文件消息会先确认收录，用户可以继续上传。

发来报告请求时，运行时会等待本次会话的来源快照处理完毕，再启动主 agent。未完成的任务会在长驻服务重启时恢复，已缓存的解析和摘要会复用。单份材料处理失败时，覆盖清单会保留失败原因。重新上传可触发重试。

## 摘要与交付

1. `prepare_source_overview(cursor=0)` 每次读取 16 项后台处理结果，持续到 `next_cursor=null`。批次记录成功的摘要和失败原因。
2. `synthesize_source_overview` 汇总所有高压缩摘要，形成整体视图和每份材料的目录。新增来源会改变清单快照。
3. `read_source_file_abstract(file_path, level, offset=0)` 读取已有的 `high`，或按需生成缺失的 `middle`、`low`。摘要按来源版本、模型配置和提示词缓存，例如 `sample.pdf.abstract.high`。较长结果用 `next_offset` 继续。
4. `read_source_evidence` 按证据编号、关键字或原文位置（如 `locator="Page 4"`）查看原文块和关联图像。报告作者可据此核对数字、单位、日期和来源。
5. `publish_report` 检查当前材料已完成总览、正文有实际内容、引用位置可解析、图片存在，再发送单个 HTML。设置 `validate_only=true` 可以先检查和渲染草稿，HTML 留在工作区。正文、图表及图片说明的引用都纳入证据附录。来源目录记录全部来源的名称、路径、校验值和处理状态。文件上限为 25 MiB 或对应接入面的更小限制。

服务启动时统一从 [config/abstract-prompts.json](../config/abstract-prompts.json) 读取三个级别的提示词。管理员可直接编辑该文件并重启服务。默认摘要调用复用主模型，独立摘要模型可用 `INSIGHTFUL_REPORT_AGENT_ABSTRACT_MODEL` 配置，两种模式都读取这一份提示词。修改某个级别的提示词会使该级别的旧摘要缓存失效。

### 各级摘要怎样生成

行文本按约 3,200 字符分块，优先沿换行切开。PDF 和 MinerU 结果按解析出来的页或结构块入库，视频使用时间位置。摘要模型按约 18,000 字符一批读取连续证据，保留每个源块的编号和位置。多组结果再以最多六组为一批递归合并。每一级都从同一份归一化证据开始，使用该级别的提示词：

| 级别 | 系统提示词侧重 | 缓存文件保留内容 |
| --- | --- | --- |
| `high` | 材料类型、核心结论或机制、关键事实和限制 | 最多 450 字符的正文，另附来源 ID 和覆盖信息。需要时用页码、图号或权利要求编号定位 |
| `middle` | 主要发现、数据口径、方法、例外和关键数字 | 最多 1,500 字符的主题摘要及少量自然定位，另附来源 ID 和覆盖信息 |
| `low` | 章节结构、分段证据、具体数据、条件与不确定性 | 更详细的总摘要和分组摘要、覆盖说明和索引 |

`low` 的完整证据及视觉索引由代码从源块生成。证据条目包含 `[[source-id#block-id]]`、原始位置和一小段预览。报告中的正式引用使用 `read_source_evidence` 返回的源块编号。工具输出按字符分页，完整摘要留在工作区。缓存命中时直接读取，同一来源的不同级别可分别生成和复用。大文件的分组摘要还会单独保存，便于重试时继续。

批量总览使用独立的一轮跨来源合并。每个成功解析的来源都贡献一份 `high` 摘要；失败或只覆盖部分内容的来源会留下状态。总览和逐份摘要目录分别留存，写作时可按证据需要继续读取。

## 为其他 agent 导出 Markdown 和索引

独立的 `SourceExporter` 接收本地输出目录、MinerU 客户端和摘要模型。可以混合传入本地文件路径与公开 HTTP(S) 链接。文件转换由 MinerU 服务执行。

```python
from insightful_report_agent.tools.mineru import MinerU
from insightful_report_agent.tools.source_export import SourceExporter

exporter = SourceExporter("./reference-export", model=summary_model, mineru=MinerU(token))
results = await exporter.prepare(
    "tests/assets/patent_docs/背部支撑装置及座椅-CN115969183A-西昊.pdf",
    "https://example.org/",
)
```

每项结果包含原件、导出、简报、内容索引和快照元数据的本地路径。内容索引通过 `result.content_index` 取得。单项失败通过 `result.error` 返回，已完成的文件可用于重试。同名原件按来源及内容版本分开存放。公开链接复用首次下载的快照，需要重新抓取时可指定一个新的 `output_dir`。

- PDF、受支持的 Office 文档、常见图片和 HTML 使用 MinerU。导出保留服务返回的 Markdown 和归档中可用的相对图片资源。MinerU 提供内容清单时，`result.evidence` 指向含页号等定位的原始 JSON。纯文本和 Markdown 等文本格式以 UTF-8 或带 BOM 的 UTF-16/UTF-32 解码。其他格式会返回处理失败，原件仍可供检查。
- `.brief.md` 只包含 front matter，字段为 `original-file-path`、`export-file-path`、`content-index-file-path` 和 `file-content-brief`。原件使用绝对路径，导出和索引使用相对于简报的路径。Harness 按 `content-index-file-path` 打开索引，再按索引中的行号或字符范围读取导出。`file-content-brief` 由摘要模型生成，目标约为导出字符数的 5%，上限 4,500 字符，短文目标下限为 40 字符。
- `.content-index.md` 提供完整导出链接、原文范围及范围摘要。长文档按每页最多八项生成子索引。交给 harness 时保留简报、主索引、子索引、`export/` 相对目录及需要引用的原件和元数据。链接的 `#L12-L30` 表示从第 12 行到第 30 行，含首尾。普通 Markdown 查看器对这类行号链接的支持各不相同。索引还提供按 Unicode 字符计数的零基半开区间，支持超长单行中的局部定位。用 `export.read_bytes().decode("utf-8")` 读取可保留换行字符及偏移。MinerU 原始 `page_idx` 从 0 开始。

做长报告时，可以让 harness 先按任务范围建立来源覆盖表，再按报告小节读取相关简报和原文范围。数字、单位、版本差异以及最终引用都回原文核对。每节写完保存草稿和证据位置，最后跨节核对结论，减少对会话上下文的依赖。

VS Code 的 Testing 面板使用 `unittest` 发现导出工具的测试。现有 pytest 测试可继续通过 `uv run pytest -q` 运行。

如需查看单元测试实际生成的 Markdown，可运行以下命令。样例会保存在 `workspace/out/source-export-tests/` 下的新目录中。测试使用真实专利 PDF 作为输入，导出内容和摘要来自模拟服务响应。

```bash
INSIGHTFUL_KEEP_EXPORT_TEST_OUTPUT=1 uv run python -m unittest -v tests.test_source_export.SourceExporterTests.test_batch_patent_pdfs_reuses_mineru_result_and_preserves_originals
```
