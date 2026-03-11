# SOR 智能合规分析平台 数据库与 API 设计 v1

> 说明：本文是早期 `v1` 设计稿，部分内容与当前实现已经不同。  
> 当前代码实际采用 `FastAPI + SQLite + 文件资产存储`，并已引入 `workspace`、preview cache、table assets、locked export 等机制。  
> 最新实现态请优先参考：`/Users/liuchang/Documents/New project/docs/项目.md`

## 1. 设计原则
- 先保证可追溯，再追求自动化率。
- AI 结果与人工结果必须分层存储，不互相覆盖。
- 所有结论必须绑定证据与版本。

## 2. 核心枚举定义

### 2.1 判定结果
- `ACCEPT`
- `REJECT`
- `DEVIATION_ACCEPT`
- `NEED_CLARIFICATION`

### 2.2 需求状态
- `DRAFT`
- `IN_REVIEW`
- `APPROVED`
- `EXPORTED`

### 2.3 需求类型
- `FUNCTIONAL`
- `PERFORMANCE`
- `INTERFACE`
- `REGULATION`
- `TEST`
- `OTHER`

## 3. 核心表结构（PostgreSQL）

## 3.1 `projects`
```sql
CREATE TABLE projects (
  id UUID PRIMARY KEY,
  code VARCHAR(64) UNIQUE NOT NULL,
  name VARCHAR(255) NOT NULL,
  customer_name VARCHAR(255) NOT NULL,
  owner_user_id UUID NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
```

## 3.2 `sor_documents`
```sql
CREATE TABLE sor_documents (
  id UUID PRIMARY KEY,
  project_id UUID NOT NULL REFERENCES projects(id),
  file_name VARCHAR(255) NOT NULL,
  file_type VARCHAR(16) NOT NULL, -- PDF/DOCX
  file_path TEXT NOT NULL,
  sor_version VARCHAR(64),
  parse_status VARCHAR(32) NOT NULL, -- PENDING/RUNNING/SUCCEEDED/FAILED
  parse_error TEXT,
  uploaded_by UUID NOT NULL,
  uploaded_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_sor_documents_project_id ON sor_documents(project_id);
```

## 3.3 `requirement_items`
```sql
CREATE TABLE requirement_items (
  id UUID PRIMARY KEY,
  project_id UUID NOT NULL REFERENCES projects(id),
  sor_document_id UUID NOT NULL REFERENCES sor_documents(id),
  req_id VARCHAR(64) NOT NULL, -- 业务可见编号
  source_page INT,
  source_section VARCHAR(255),
  source_anchor TEXT, -- 段落定位或表格定位
  customer_requirement_text TEXT NOT NULL,
  requirement_type VARCHAR(32) NOT NULL,
  priority VARCHAR(16), -- HIGH/MEDIUM/LOW
  status VARCHAR(32) NOT NULL DEFAULT 'DRAFT',
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
  UNIQUE (project_id, req_id)
);
CREATE INDEX idx_requirement_items_project_status ON requirement_items(project_id, status);
CREATE INDEX idx_requirement_items_document_id ON requirement_items(sor_document_id);
```

## 3.4 `requirement_assessments`
```sql
CREATE TABLE requirement_assessments (
  id UUID PRIMARY KEY,
  requirement_item_id UUID NOT NULL REFERENCES requirement_items(id),
  ai_proposed_decision VARCHAR(32),        -- AI建议
  ai_reason TEXT,
  ai_confidence NUMERIC(5,4),              -- 0~1
  final_decision VARCHAR(32),              -- 人工最终结果
  engineer_reason TEXT,                    -- 人工备注
  decided_by UUID,                         -- 人工判定人
  decided_at TIMESTAMP,
  model_name VARCHAR(128),
  prompt_version VARCHAR(64),
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX uk_assessment_requirement_item ON requirement_assessments(requirement_item_id);
```

## 3.5 `requirement_evidence`
```sql
CREATE TABLE requirement_evidence (
  id UUID PRIMARY KEY,
  requirement_item_id UUID NOT NULL REFERENCES requirement_items(id),
  source_doc_id UUID NOT NULL,             -- knowledge_documents.id
  source_doc_name VARCHAR(255) NOT NULL,
  source_doc_version VARCHAR(64),
  source_location VARCHAR(255),            -- 章节/页码/段落
  excerpt TEXT NOT NULL,
  relevance_score NUMERIC(5,4),
  is_primary BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_requirement_evidence_requirement ON requirement_evidence(requirement_item_id);
```

## 3.6 `knowledge_documents`
```sql
CREATE TABLE knowledge_documents (
  id UUID PRIMARY KEY,
  title VARCHAR(255) NOT NULL,
  doc_type VARCHAR(64) NOT NULL,           -- SPEC/STANDARD/HISTORY/TEST
  version VARCHAR(64) NOT NULL,
  status VARCHAR(32) NOT NULL,             -- DRAFT/PUBLISHED/ARCHIVED
  effective_from DATE,
  effective_to DATE,
  file_path TEXT NOT NULL,
  uploaded_by UUID NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_knowledge_documents_status ON knowledge_documents(status);
```

## 3.7 `review_logs`
```sql
CREATE TABLE review_logs (
  id UUID PRIMARY KEY,
  requirement_item_id UUID NOT NULL REFERENCES requirement_items(id),
  action VARCHAR(64) NOT NULL,             -- ACCEPT_AI/EDIT_DECISION/REJECT_AI/REOPEN
  old_value JSONB,
  new_value JSONB,
  comment TEXT,
  operator_user_id UUID NOT NULL,
  operated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_review_logs_requirement_time ON review_logs(requirement_item_id, operated_at DESC);
```

## 3.8 `export_jobs`
```sql
CREATE TABLE export_jobs (
  id UUID PRIMARY KEY,
  project_id UUID NOT NULL REFERENCES projects(id),
  format VARCHAR(16) NOT NULL,             -- XLSX/PDF
  status VARCHAR(32) NOT NULL,             -- PENDING/RUNNING/SUCCEEDED/FAILED
  output_path TEXT,
  error_message TEXT,
  created_by UUID NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMP
);
```

## 4. 状态流转
1. `DRAFT`: 系统拆解后初始状态。
2. `IN_REVIEW`: 工程师开始处理。
3. `APPROVED`: Lead 审核通过，锁定关键字段。
4. `EXPORTED`: 导出后状态。
5. `REOPEN` 动作可把 `APPROVED/EXPORTED` 打回 `IN_REVIEW`（必须填原因）。

## 5. API 清单（REST）

## 5.1 项目与文档
- `POST /api/v1/projects`
- `GET /api/v1/projects`
- `POST /api/v1/projects/{projectId}/sor-documents` (multipart)
- `GET /api/v1/projects/{projectId}/sor-documents`
- `POST /api/v1/sor-documents/{docId}/parse`

## 5.2 需求拆解与查询
- `POST /api/v1/sor-documents/{docId}/requirements/extract`
- `GET /api/v1/projects/{projectId}/requirements`
- `PATCH /api/v1/requirements/{requirementId}` (支持文本、类型、优先级、状态更新)
- `POST /api/v1/requirements/{requirementId}/split`
- `POST /api/v1/requirements/{requirementId}/merge`

## 5.3 AI 判定与证据
- `POST /api/v1/projects/{projectId}/assessments/run` (批量运行)
- `POST /api/v1/requirements/{requirementId}/assess` (单条重跑)
- `GET /api/v1/requirements/{requirementId}/assessment`
- `GET /api/v1/requirements/{requirementId}/evidence`

## 5.4 人工复核与审批
- `POST /api/v1/requirements/{requirementId}/decision`
- `POST /api/v1/requirements/{requirementId}/approve`
- `POST /api/v1/requirements/{requirementId}/reopen`
- `GET /api/v1/requirements/{requirementId}/review-logs`

## 5.5 知识库
- `POST /api/v1/knowledge/documents`
- `GET /api/v1/knowledge/documents`
- `POST /api/v1/knowledge/documents/{docId}/publish`
- `POST /api/v1/knowledge/reindex`

## 5.6 导出
- `POST /api/v1/projects/{projectId}/exports`
- `GET /api/v1/projects/{projectId}/exports`
- `GET /api/v1/exports/{exportId}/download`

## 6. 关键 API 示例

## 6.1 人工提交最终判定
`POST /api/v1/requirements/{requirementId}/decision`

```json
{
  "finalDecision": "DEVIATION_ACCEPT",
  "engineerReason": "现有架构可满足主体功能，但接口响应时间需放宽至120ms。",
  "comment": "已与平台组确认资源预算。"
}
```

响应:
```json
{
  "requirementId": "6c74d1dd-1299-4b65-8af4-73df6d95f4f5",
  "status": "IN_REVIEW",
  "finalDecision": "DEVIATION_ACCEPT",
  "updatedAt": "2026-02-19T12:30:00Z"
}
```

## 6.2 获取单条需求完整视图
`GET /api/v1/requirements/{requirementId}/assessment`

```json
{
  "requirement": {
    "reqId": "REQ-1.2.3",
    "text": "The ECU shall support wake-up time less than 100ms."
  },
  "aiAssessment": {
    "proposedDecision": "REJECT",
    "reason": "当前平台在低温工况下启动时间统计P95为132ms。",
    "confidence": 0.84
  },
  "finalAssessment": {
    "decision": "DEVIATION_ACCEPT",
    "reason": "与客户沟通建议修订为<=150ms，待确认。"
  },
  "evidence": [
    {
      "doc": "Platform_Performance_Spec",
      "version": "v3.2",
      "location": "Sec 4.1 / p.27",
      "excerpt": "Cold start latency P95 = 132ms."
    }
  ]
}
```

## 7. 异步任务与事件（建议）
- 任务:
  - 文档解析任务
  - 批量 AI 判定任务
  - 知识库重建索引任务
  - 导出任务
- 事件:
  - `DocumentParsed`
  - `RequirementsExtracted`
  - `AssessmentCompleted`
  - `ExportReady`

## 8. 监控与审计
- 指标:
  - 解析成功率、平均解析时长
  - AI 判定覆盖率、低置信度占比
  - 人工改写率、导出成功率
- 审计:
  - 所有判定动作入 `review_logs`
  - 关键接口保留请求追踪ID
