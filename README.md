# 多层物料清单发布

维护带**生效区间**的多层 BOM（总成/子件/用量/损耗/替代关系），发布前做完整性校验，
签署后冻结完整依赖快照，并支持按任意历史日期展开需求、逐项说明用量来源。

本项目维护多层物料清单发布的领域约定、角色边界与样例数据，供后端服务、接口和自动化
验证统一使用。领域契约覆盖采购计划员、供应商、质量工程师、仓储管理员，并明确
**多层依赖图、发布完整性校验、依赖快照冻结、历史日期展开**四个关键约束。

## 解决的问题

新车型试产时工程部门同时修改总成、子件和替代料关系，若父级先发布而下层仍是草稿，
需求展开会不一致。本后端在发布时强制：

- 下层在签署日**没有生效的已发布版本**（仍为草稿）→ 阻断（`LOWER_LEVEL_DRAFT`）；
- 下层存在生效区间覆盖签署日的**在途草稿/待确认版本** → 阻断，逼出发布顺序；
- 依赖图存在循环（含替代料边）→ 阻断（`CYCLE`）；
- 子件/替代料主数据缺失（`MISSING_PART`）、用量单位无法换算（`UNIT_NOT_CONVERTIBLE`）、
  用量非正、损耗越界、区间倒挂 → 阻断。

已签署版本**不可变**；紧急更正、分支合并、部件停用、并发发布全部生成新版本。

## 领域模型

| 概念 | 说明 |
| --- | --- |
| `Part` | 物料主数据：编码、名称、主单位、单位换算、停用日期 |
| `Revision` | 总成 BOM 版本：草拟→待确认→已发布（→已作废），半开生效区间 `[valid_from, valid_to)` |
| `Line` | 用量行：子件、每父件定额、用量单位、损耗率、生效区间 |
| `Substitute` | 替代关系：替代料、比例、单位、优先级、生效区间 |
| `Snapshot` | 签署时递归冻结的完整依赖闭包（版本内容 + 部件主数据 + 单位换算），SHA-256 校验 |

关键规则：

- **快照冻结**：已发布版本的多层展开永远以签署时钉住的下层版本与用量为准；
  下层后续改版/出草稿不影响旧版展开。单位换算也随快照冻结。
- **历史展开**：根版本按展开日选择生效的已发布版本；用量行/替代关系按各自生效区间过滤；
  原子件在展开日已停用时，自动选用当日生效、优先级最高的替代料（停用状态是带日期的
  主数据事实，按展开日实时判定）。
- **版本接替**：新版本签署时自动把开放式旧版截止到新生效日；同日并发发布无法区分
  先后，返回 `VERSION_CONFLICT`（409），必须基于最新基线重新起草。
- 单位换算系数语义：`1 目标单位 = factor 主单位`（主单位 g 时 `{"kg": 1000}`）。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/bom_service/`：BOM 后端
  - `models.py` 模型与状态机；`units.py` 单位换算图；`store.py` SQLite 仓储
  - `validation.py` 发布前校验；`freeze.py` 快照冻结；`explode.py` 需求展开与来源
  - `service.py` 用例编排；`api.py` JSON HTTP 接口
- `tools/check_contract.py`：契约命令行摘要检查。
- `tools/bom_demo.py`：端到端业务场景演示（含"父级先发布被阻断"全过程）。
- `tests/`：契约回归 + BOM 领域回归（19 个用例）。

## 快速开始

```bash
# 端到端场景演示（无需数据库，内存态）
PYTHONPATH=src python3 tools/bom_demo.py

# 启动 HTTP 服务（仅依赖 Python 3.11+ 标准库）
PYTHONPATH=src python3 -m bom_service.api --db bom.db --port 8080
```

## API 摘要

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/parts` | 建立/更新部件主数据（含单位换算） |
| GET | `/api/parts` | 部件清单 |
| POST | `/api/parts/{code}/conversion` | 追加单位换算 |
| POST | `/api/parts/{code}/obsolete` | 部件停用（自动为受影响已发布总成开新版本草稿） |
| POST | `/api/products/{code}/revisions` | 起草版本 |
| GET | `/api/products/{code}/revisions` | 版本列表 |
| PUT | `/api/revisions/{code}/lines?branch=&v=` | 设置用量行（用量、单位、损耗、区间） |
| PUT | `/api/revisions/{code}/substitutes/{child}?branch=&v=` | 设置替代关系 |
| GET | `/api/revisions/{code}/validate?branch=&v=` | 发布前校验（不改变状态） |
| POST | `/api/revisions/{code}/submit?branch=&v=` | 提交签署（草拟→待确认） |
| POST | `/api/revisions/{code}/release?branch=&v=` | 签署发布（锁内复核+冻结快照） |
| POST | `/api/revisions/{code}/emergency-correction?v=` | 紧急更正（复制内容出新草稿，旧版截止） |
| POST | `/api/products/{code}/branches/{name}?v=` | 从已发布版本拉试制分支 |
| POST | `/api/branches/{code}/{name}/merge?v=` | 分支合并回 main（生成 main 新版本） |
| GET | `/api/explode/{code}?date=&qty=&branch=` | 按日期展开需求（树+汇总+逐项来源） |
| GET | `/api/snapshots` / `/api/snapshots/{code}?branch=&v=` | 快照清单/详情 |

展开结果中每一笔用量都带 `provenance`/`sources`：出处版本、用量行定额、损耗率、
单位换算系数、计算公式、用量行生效区间及替代决策原因。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
