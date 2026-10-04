# 多层物料清单发布

试产期工程部门同时修改总成、子件与替代料关系时，采购侧曾出现“父级版本已发布、
下层仍引用草稿”，导致需求展开不一致。本项目在领域契约之上建设 Python 后端：

- 维护带**生效区间**（半开 `[valid_from, valid_to)`）的多层 BOM、用量、损耗率与替代关系；
- **发布前校验**：循环依赖、缺失依赖（含“下层仅有草稿”）、部件停用、单位量纲换算；
- **签署即冻结完整依赖快照**：每个子件固定到签署时刻已发布的精确版本（SHA-256 摘要），
  草稿永远不可能被上层引用，快照不可变；
- **任意日期展开需求**，默认沿签署快照逐层固定版本（结果可复现），
  可选 `current` 模式按当日最新已发布版本重算；每项用量都附完整来源链
  （来源版本、行号、单位用量、损耗率、换算系数、父件需求、是否替代引入）；
- **紧急更正、分支合并、部件停用、并发发布**均产生新版本，绝不改写已冻结历史。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/bom_release/`：BOM 发布后端
  - `models.py`：物料、单位、BOM 版本、替代料、冻结快照；
  - `units.py`：同量纲单位注册与换算；
  - `store.py`：内存仓储与 JSON 持久化；
  - `validation.py`：循环/缺失/草稿引用/停用/单位换算校验与快照固化；
  - `service.py`：草稿、签署、紧急更正、分支三路合并、停用影响、乐观锁与区间互斥；
  - `explosion.py`：任意日期多层展开与用量来源追溯；
  - `api.py`：标准库 HTTP API（零三方依赖）。
- `tools/check_contract.py`：契约命令行摘要。
- `tools/serve.py`：启动 HTTP 服务（无需设置 PYTHONPATH）。
- `tools/smoke_http.py`：HTTP 端到端冒烟脚本。
- `tests/`：契约回归与 BOM 后端端到端测试。

## 快速开始

```bash
python3 tools/serve.py --port 8080          # 或 python3 -m bom_release（需 PYTHONPATH=src）
# 带持久化：python3 tools/serve.py --data store.json
```

主要端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/units` `/api/materials` | 注册单位、物料 |
| POST | `/api/boms` | 建草稿（可 `based_on` 复制旧版本） |
| PUT | `/api/boms/{code}/lines` | 修订行（携带 `expected_seq` 乐观锁） |
| GET | `/api/boms/{code}/precheck` | 发布前校验（循环/缺失/草稿/停用/单位） |
| POST | `/api/boms/{code}/sign` | 签署并冻结完整依赖快照 |
| GET | `/api/boms/{code}/snapshot` | 查看冻结快照与摘要 |
| POST | `/api/boms/{code}/emergency-correct` | 紧急更正（裁剪旧版本尾部区间，发新版本） |
| POST | `/api/materials/{m}/branches`、`/merge` | 建分支 / 三路合并回 main |
| POST | `/api/materials/{code}/discontinue` | 停用并列出受影响的已发布父级 |
| GET | `/api/materials/{code}/impacted` | 已发布快照中引用该件的全部父级版本 |
| GET | `/api/explode?material=&qty=&date=&mode=snapshot\|current` | 按日期展开并说明每项用量来源 |

冲突语义：422 = 发布校验失败（附全部问题）；409 = 乐观锁/分支合并冲突；
404 = 不存在；400 = 请求错误。

## 关键不变量

1. **多层依赖图**：循环检测覆盖已冻结快照与待发布草稿（含替代料边）。
2. **发布完整性校验**：下层只有草稿、缺生效版本、引用停用件、单位量纲不一致均拦截。
3. **依赖快照冻结**：签署后父级直接/间接依赖全部解析为精确已发布版本并哈希；
   已签署版本不可编辑，旧区间只可被新版本裁剪尾部，不可回溯改写。
4. **历史日期展开**：`snapshot` 模式按签署口径复现任意历史日期；
   `current` 模式只重新选择已发布版本，草稿始终不参与。

## 验证

```bash
python3 -m unittest discover -s tests -v     # 契约回归 + BOM 后端共 20 项测试
python3 -m compileall -q src tools tests     # 编译检查
python3 tools/check_contract.py domain/contract.json
python3 tools/smoke_http.py                  # 需先启动 tools/serve.py --port 8091
```
