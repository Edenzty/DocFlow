"""Exercise the real document lifecycle and SQLite persistence, with no LLM calls."""
from pathlib import Path
import json

from .config import Settings
from .storage import Conflict
from .workflow import Engine


def run_demo(settings: Settings, output: Path):
    output = Path(output)
    if output.exists():
        raise ValueError("演示输出目录已存在，请换新目录保留记录")
    output.mkdir(parents=True)
    engine = Engine(Settings(root=settings.root, data_dir=output / "state"))
    checks = []
    doc = {"document_id": "demo-queue", "title": "演示网关维护手册", "source": "原创合成演示手册",
           "filename": "queue.md", "content": "# DFX900队列积压\n设备接口出现DFX900时，先查看队列长度，重试间隔配置为15秒。\n"}
    question = "设备接口出现DFX900时重试间隔配置为多少秒？"
    try:
        initial = engine.knowledge_summary()
        staged = engine.knowledge.import_document(**doc)
        assert engine.knowledge_summary()["snapshot_id"] == initial["snapshot_id"]
        assert not any(x["document_id"] == doc["document_id"] for x in engine.search(question, "keyword"))
        checks.append("导入与预览不会影响在线检索")
        v1 = engine.publish_knowledge(staged["version_id"], initial["snapshot_id"])
        old_ticket = engine.create(question)
        assert old_ticket["knowledge_snapshot_id"] == v1["snapshot_id"]
        assert any(x["document_id"] == doc["document_id"] and "15秒" in x["text"] for x in old_ticket["evidence"])
        checks.append("发布后真实BM25检索命中新文档，工单绑定该知识快照")
        doc["content"] = doc["content"].replace("15秒", "45秒")
        staged2 = engine.knowledge.import_document(**doc)
        try:
            engine.publish_knowledge(staged2["version_id"], initial["snapshot_id"])
            raise AssertionError("过期发布请求不应成功")
        except Conflict:
            checks.append("过期发布请求冲突，当前版本保持不变")
        v2 = engine.publish_knowledge(staged2["version_id"], v1["snapshot_id"])
        search = engine.search_traced(question, "keyword")
        imported = [x for x in search["results"] if x["document_id"] == doc["document_id"]]
        assert imported and all("45秒" in x["text"] and "15秒" not in x["text"] for x in imported)
        checks.append("更新后新查询只读取该文档的新修订，旧片段不混入")
        old = engine.store.get(old_ticket["id"])
        assert old["knowledge_snapshot_id"] == v1["snapshot_id"]
        assert any("15秒" in x["text"] for x in old["evidence"])
        checks.append("已有草稿和证据保留原快照，不被新文档重写")
        updated = engine.followup(old["id"], "请按当前手册重新确认配置")
        assert updated["knowledge_snapshot_id"] == v2["snapshot_id"]
        assert any(v["knowledge_snapshot_id"] == v1["snapshot_id"] for v in updated["versions"])
        checks.append("明确追问生成新草稿使用当前快照，历史草稿仍可追溯")
        traces = engine.store.list_retrievals(old["id"])
        assert {x["snapshot_id"] for x in traces} >= {v1["snapshot_id"], v2["snapshot_id"]}
        assert all(x["results"] and x["results"][0]["rank"] == 1 for x in traces)
        checks.append("逐次检索保存快照、查询、方法、排名、正文与耗时")
        rolled = engine.activate_knowledge(v1["snapshot_id"], v2["snapshot_id"])
        assert any("15秒" in x["text"] for x in engine.search(question, "keyword") if x["document_id"] == doc["document_id"])
        checks.append("回滚后新检索恢复旧修订")
        engine.remove_knowledge(doc["document_id"], rolled["snapshot_id"])
        assert not any(x["document_id"] == doc["document_id"] for x in engine.search(question, "keyword"))
        checks.append("退役文档后不再进入新查询，历史证据仍保留")
        engine.close()
        engine = Engine(Settings(root=settings.root, data_dir=output / "state"))
        assert engine.store.get_retrieval(search["trace"]["trace_id"])["snapshot_id"] == v2["snapshot_id"]
        assert any(x["version_id"] == staged2["version_id"] for x in engine.knowledge.document_versions(doc["document_id"]))
        checks.append("服务重启后修订、快照和检索记录仍然存在")
        result = {"mode": "real_document_lifecycle_with_mock_generation", "model_requests": 0,
                  "passed": len(checks), "checks": checks,
                  "example": {"question": question, "old_snapshot": v1["snapshot_id"], "new_snapshot": v2["snapshot_id"],
                              "trace": search["trace"], "ticket": engine.store.get(old["id"])}}
        (output / "demo.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result
    finally:
        engine.close()
