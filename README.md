# 多源数据采集与RPA自动化平台

面向网页抓取、API接入、增量采集、RPA流程编排、数据校验与采集监控的数据工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据源配置 | `datasource` | 数据源 | 源端编号、连接类型、接入地址 |
| 网页采集规则 | `crawler` | 采集规则 | 规则编号、目标网址、页面选择器 |
| API接入管理 | `apiclient` | API客户端 | 客户端编号、接口名称、请求方法 |
| 增量采集 | `incremental` | 增量游标 | 游标编号、来源标识、增量字段 |
| RPA任务编排 | `robot_task` | RPA任务 | 任务编号、任务名称、页面定位器 |
| 数据接入通道 | `ingestion` | 接入通道 | 通道编号、通道名称、输入格式 |
| 格式解析器 | `parser` | 解析配置 | 解析编号、输入格式、输出格式 |
| 采集结果校验 | `validator` | 校验规则 | 规则编号、校验字段、校验类型 |
| 采集去重 | `dedup` | 去重策略 | 策略编号、输入来源、去重键 |
| 采集调度 | `schedule` | 调度计划 | 计划编号、关联任务、调度周期 |
| 采集监控 | `monitor` | 监控面板 | 面板编号、采集源总数、今日采集量 |
| 采集日志 | `log_viewer` | 日志条目 | 日志编号、关联任务、日志级别 |
| 凭证管理 | `auth` | 认证凭证 | 凭证编号、凭证类型、关联数据源 |
| 采集转换管道 | `transform` | 转换节点 | 节点编号、输入来源、输出目标 |
| 数据流转编排 | `flow` | 流转管道 | 管道编号、管道名称、起始节点 |
| 数据血缘追踪 | `lineage` | 血缘关系 | 关系编号、上游节点、下游节点 |
| 采集报告 | `report` | 采集报告 | 报告编号、采集源、统计周期 |
| 采集登记迁移 | `migration`（SQLite 独立库） | 迁移对账、来源地址版本、待执行计划、采样领取 | 源端编号、计划编号、绑定状态、地址版本 |

## 采集登记迁移（SQLite）

数据源配置与采集计划的存量从内存样本表迁移到独立的 SQLite 库（默认
`backend/data/migration.db`，可用环境变量 `MIGRATION_DB` 覆盖），原内存模块不受影响。

- 对账数据集：`POST /api/migration/reconciliation/build` 从样本表构建，区分
  `LANDED 已落库` 与 `REVIEW 待核对`；重复构建只补缺，已确认记录与源端地址不会被种子覆盖。
- 绑定口径：仅承认“关联任务与源端编号完全一致”的精确匹配，或值班员通过
  `POST /api/migration/mappings` 提供的明确任务与来源对照；没有对应源的计划只留核对记录，
  不会分配到同编号的其他来源。
- 地址更正：`POST /api/migration/sources/{源端编号}/address-correction` 生成新确认版本；
  待执行计划保留生成/受理时确认的地址快照，采样领取（`POST /api/migration/dispatch/claim-next`）
  会说明实际使用的是哪一版地址。
- 旧阶段兼容：`POST /api/migration/legacy/intake` 继续接收状态/排期记录，是否向旧登记处
  报送由 `GET|POST /api/migration/legacy/reporting` 控制；原编号、原状态可通过
  `GET /api/migration/compatibility/{编号}` 查询。
- 受理与未派发解释：`GET /api/migration/plans/{计划编号}/admission` 查此刻可否受理及
  采样地址版本；`GET /api/migration/plans/{计划编号}/undispatched` 解释无来源计划为何不派发，
  数据全部落 SQLite，进程重启后读到同一份已确认版本。

核验脚本（不依赖第三方库，使用项目 venv 即可）：

```bash
cd backend
.venv/bin/python -I scripts/migration_scenario.py     # 服务层全场景（46 项）
# 另起终端启动服务后：
.venv/bin/python -I scripts/migration_http_check.py  # HTTP 接口全链路
```

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
