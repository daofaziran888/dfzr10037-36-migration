"""推荐引擎候选限额 v2 迁移与解码的验收脚本（纯标准库断言，直接运行即可）。

运行：cd backend && .venv/bin/python tests/test_reco_candidates.py

覆盖：
1. 示例数据候选数量为占位字符串（迁移前前提）；
2. 存量版本迁移：幂等、原字面与来源可追溯、未知值不落零不放全量；
3. 旧值能转换时沿用真实数量含义（整数与数字字符串）；
4. 运行时两种来源（推理/探查）写入同一推荐清单，直接落 v2；
5. 同一源实体新旧两种结果不重复计入，老规则搁置原因可复核；
6. 实际证据数量为 0 的规则不继续使用；
7. 解码结果同步路径推理待处理清单且幂等；
8. 旧值回填与新记录写入并发执行安全；
9. 回退旧版（version=1）读取：纳入/搁置可查，原字面与来源可追溯。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import service_for  # noqa: E402
from app.services.reco_engine import LIMIT_FIELD, migrate_stock  # noqa: E402
from app.store import store  # noqa: E402

reco = service_for("reco_engine")
path_reason = service_for("path_reason")
subgraph_exp = service_for("subgraph_exp")

CHECKS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  | {detail}" if detail else ""))


# ---------- 1. 迁移前前提：示例数据是占位字符串 ----------
seed_rows = store.rows("reco_engine")
check(
    "示例数据候选数量为占位字符串",
    all(isinstance(r["候选数量"], str) and r["候选数量"].startswith("推荐引擎样例") for r in seed_rows),
    f"候选数量={[r['候选数量'] for r in seed_rows]}",
)

# ---------- 2. 注入两条“旧版运行时”存量（无 v2 限额），模拟上线前写入 ----------
store.rows("reco_engine").append({
    "id": 91, "status": "已生效", "pending": True, "abnormal": False,
    "规则编号": "RECO-0091", "推荐场景": "存量运行时", "源实体": "实体-存量-int",
    "推荐算法": "legacy-rank", "候选数量": 25, "过滤条件": "", "命中次数": "3", "采纳率": "0%",
})
store.rows("reco_engine").append({
    "id": 92, "status": "已生效", "pending": True, "abnormal": False,
    "规则编号": "RECO-0092", "推荐场景": "存量运行时", "源实体": "实体-存量-str",
    "推荐算法": "legacy-rank", "候选数量": "18", "过滤条件": "", "命中次数": "5", "采纳率": "0%",
})

stats1 = migrate_stock()
check("首次迁移覆盖全部存量", stats1["migrated"] == 5 and stats1["total"] == 5, str(stats1))
stats2 = migrate_stock()
check("重复迁移幂等（不再改动）", stats2["migrated"] == 0 and stats2["skipped"] == 5, str(stats2))

by_no = {r["规则编号"]: r for r in store.rows("reco_engine")}
lim1 = by_no["RECO-0001"][LIMIT_FIELD]
check(
    "占位字符串迁移为未知（不落零、来源 legacy、原字面留档）",
    lim1["value"] is None and lim1["source"] == "legacy" and lim1["raw"] == "推荐引擎样例1",
    str(lim1),
)
lim91 = by_no["RECO-0091"][LIMIT_FIELD]
lim92 = by_no["RECO-0092"][LIMIT_FIELD]
check(
    "可转换旧值沿用真实数量含义（25 与 \"18\"）",
    lim91["value"] == 25 and lim91["evidence"] == 25 and lim92["value"] == 18 and lim92["source"] == "legacy",
    f"RECO-0091={lim91['value']}, RECO-0092={lim92['value']}",
)
check("迁移不改写原字段", by_no["RECO-0001"]["候选数量"] == "推荐引擎样例1" and by_no["RECO-0091"]["候选数量"] == 25)

# ---------- 3. 运行时两种来源写入同一推荐清单 ----------
_, msg_sub = subgraph_exp.run_action(1, "启动探查")
check("探查推送推荐规则", "RECO-SUB-" in msg_sub, msg_sub)

task_a, _ = path_reason.create_entry({
    "任务编号": "PATH-T-001", "起始实体": "推荐引擎样例2", "目标实体": "目标A",
    "推理模式": "bfs", "路径数量": 7,
})
_, msg_path = path_reason.run_action(int(task_a["id"]), "取消推理")  # → 已完成 → 推送
check("推理推送推荐规则", "RECO-PATH-" in msg_path, msg_path)

task_b, _ = path_reason.create_entry({
    "任务编号": "PATH-T-002", "起始实体": "实体-零证据", "目标实体": "目标B",
    "推理模式": "dfs", "路径数量": 0,
})
path_reason.run_action(int(task_b["id"]), "取消推理")

by_no = {r["规则编号"]: r for r in store.rows("reco_engine")}
sub_no = next(no for no in by_no if no.startswith("RECO-SUB-"))
path_no = next(no for no in by_no if no.startswith("RECO-PATH-") and by_no[no]["源实体"] == "推荐引擎样例2")
zero_no = next(no for no in by_no if no.startswith("RECO-PATH-") and by_no[no]["源实体"] == "实体-零证据")
sub_lim = by_no[sub_no][LIMIT_FIELD]
path_lim = by_no[path_no][LIMIT_FIELD]
check(
    "探查带入数量落 v2（source=probe, evidence=10）",
    sub_lim["value"] == 10 and sub_lim["source"] == "probe" and sub_lim["evidence"] == 10,
    str(sub_lim),
)
check(
    "推理生成整数落 v2（source=inference, 12=7+5, evidence=7）",
    path_lim["value"] == 12 and path_lim["source"] == "inference" and path_lim["evidence"] == 7,
    str(path_lim),
)

# ---------- 4. v2 候选解码：纳入 / 搁置 / 去重 / 证据 ----------
decode = reco.decode_candidates()
inc = {v["规则编号"]: v for v in decode["included"]}
she = {v["规则编号"]: v for v in decode["shelved"]}
check(
    "纳入集合=推理12+探查10+存量25+存量18",
    decode["total_limit"] == 65 and decode["candidate_rule_count"] == 4,
    f"total={decode['total_limit']} included={sorted(inc)}",
)
check("老规则 RECO-0002 被去重搁置且指向新记录", "RECO-0002" in she and she["RECO-0002"].get("dedup_of") == path_no,
      she.get("RECO-0002", {}).get("reason", ""))
check("找不到探查结果的老规则按未知搁置", "RECO-0001" in she and "未知" in she["RECO-0001"]["reason"]
      and "RECO-0003" in she, she.get("RECO-0001", {}).get("reason", ""))
check("未知值没有变成 0 或全量候选", all(v["value"] is None for v in she.values() if "未知" in v["reason"])
      and "RECO-0001" not in inc)
check("证据数量为 0 的规则不继续使用", zero_no in she and "证据" in she[zero_no]["reason"], she.get(zero_no, {}).get("reason", ""))
check("新旧两种结果只计入一条", sum(1 for v in decode["included"] + decode["shelved"] if v["源实体"] == "推荐引擎样例2") == 2
      and sum(1 for v in decode["included"] if v["源实体"] == "推荐引擎样例2") == 1)

# ---------- 5. 解码结果同步路径推理待处理清单（幂等） ----------
sync1 = reco.sync_decode_to_path_reason(decode)
sync2 = reco.sync_decode_to_path_reason(decode)
deco_tasks = [r for r in store.rows("path_reason") if str(r.get("任务编号", "")).startswith("PATH-DECO-")]
check("同步 4 条解码任务到待处理清单", sync1 == {"synced": 4, "skipped": 0} and len(deco_tasks) == 4, str(sync1))
check("重复同步不重复写入", sync2 == {"synced": 0, "skipped": 4}, str(sync2))
check(
    "同步任务为待处理且带实际证据数量",
    all(t["pending"] and t["status"] == "待推理" for t in deco_tasks)
    and {t["任务编号"]: t["路径数量"] for t in deco_tasks}[f"PATH-DECO-{path_no}"] == 7,
)

# ---------- 6. 并发：旧值回填 × 新记录写入 × 解码 ----------
sub_ids, path_ids = [], []
for i in range(6):
    e, _ = subgraph_exp.create_entry({
        "配置编号": f"SUBG-C-{i:03d}", "探查种子": f"并发探查{i}", "扩展策略": "bfs", "结果数量": i + 1,
    })
    sub_ids.append(int(e["id"]))
    e, _ = path_reason.create_entry({
        "任务编号": f"PATH-C-{i:03d}", "起始实体": f"并发推理{i}", "目标实体": "并发目标",
        "推理模式": "bfs", "路径数量": i + 1,
    })
    path_ids.append(int(e["id"]))

errors: list[str] = []


def guard(fn):
    def wrap():
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
    return wrap


def loop_migrate():
    for _ in range(40):
        migrate_stock()


def loop_decode():
    for _ in range(40):
        reco.decode_candidates()


threads = [threading.Thread(target=guard(loop_migrate)) for _ in range(2)]
threads += [threading.Thread(target=guard(loop_decode)) for _ in range(2)]
threads += [threading.Thread(target=guard(lambda i=i: subgraph_exp.run_action(i, "启动探查"))) for i in sub_ids]
threads += [threading.Thread(target=guard(lambda i=i: path_reason.run_action(i, "取消推理"))) for i in path_ids]
for t in threads:
    t.start()
for t in threads:
    t.join()

rows = store.rows("reco_engine")
ids = [int(r["id"]) for r in rows]
nos = [r["规则编号"] for r in rows]
check("并发执行无异常", not errors, ";".join(errors[:3]))
check("并发后全部记录均为 v2 限额", all(isinstance(r.get(LIMIT_FIELD), dict) and r[LIMIT_FIELD]["version"] == 2 for r in rows))
check("并发下 id 与规则编号唯一", len(set(ids)) == len(ids) and len(set(nos)) == len(nos))
d1, d2 = reco.decode_candidates(), reco.decode_candidates()
check("并发后解码结果稳定", d1["total_limit"] == d2["total_limit"] and d1["candidate_rule_count"] == d2["candidate_rule_count"],
      f"total={d1['total_limit']}")
check("并发后迁移幂等", migrate_stock()["migrated"] == 0)

# ---------- 7. 回退旧版读取（version=1） ----------
rolled = reco.decode_candidates(version=1)
inc1 = {v["规则编号"]: v for v in rolled["included"]}
she1 = {v["规则编号"]: v for v in rolled["shelved"]}
check("旧版读取返回纳入与搁置", rolled["version"] == 1 and inc1 and she1)
check(
    "旧版读取可追到原字面与来源",
    she1["RECO-0001"]["raw"] == "推荐引擎样例1" and she1["RECO-0001"]["source"] == "legacy"
    and inc1["RECO-0091"]["raw"] == 25 and inc1["RECO-0091"]["limit"] == 25
    and inc1[path_no]["source"] == "inference",
)
check("旧版读取不触发迁移改写", by_no["RECO-0001"]["候选数量"] == "推荐引擎样例1")
check(
    "回滚后新版读取仍一致",
    reco.decode_candidates()["total_limit"] == d1["total_limit"],
)

# ---------- 汇总 ----------
failed = [c for c in CHECKS if not c[1]]
print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过")
if failed:
    print("未通过：", [c[0] for c in failed])
    sys.exit(1)
