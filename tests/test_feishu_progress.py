from insightful_report_agent.surfaces.feishu.progress import step_phrase


def test_source_image_review_labels_do_not_expose_paths_or_question() -> None:
    paths = [
        "/workspace/source-library/test/assets/a0001.jpg",
        "source-library/test/assets/a0002.jpg",
    ]
    assert step_phrase("look", {"paths": paths, "question": "Private text"}) == (
        "正在用视觉模型检查来源附图（本批 2 张）",
        "已完成来源附图检查（本批 2 张）",
    )
    assert step_phrase("look", {"paths": ["/workspace/projects/preview.png"]}) == (
        "正在用视觉模型检查图片（本批 1 张）",
        "已完成图片检查（本批 1 张）",
    )


def test_report_workspace_steps_use_descriptive_labels() -> None:
    assert (
        step_phrase(
            "execute",
            {"command": "ls -la /workspace/tools /workspace/references /workspace/brand"},
        )[1]
        == "已查看报告工具和参考资料"
    )
    assert step_phrase("execute", {"command": "ls /workspace/projects"})[1] == "已查看工作区目录"
    assert (
        step_phrase(
            "read_file", {"file_path": "/workspace/source-library/overviews/test/catalog.md"}
        )[1]
        == "已阅读来源覆盖清单"
    )
    assert step_phrase("read_file", {"file_path": "/workspace/tools/source_report.py"})[1] == (
        "已阅读报告渲染脚本"
    )
