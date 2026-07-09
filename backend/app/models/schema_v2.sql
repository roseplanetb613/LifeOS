-- ═══════════════════════════════════════════════
-- LifeOS Memory Schema v2
-- 三层存储：原始消息(MySQL) → 对话摘要(MySQL) → 长期记忆(ChromaDB)
-- ═══════════════════════════════════════════════

-- ──────────────────────────
-- 1. 对话元数据
-- ──────────────────────────
CREATE TABLE conversations (
    id              CHAR(32)     PRIMARY KEY,
    title           VARCHAR(256) NOT NULL DEFAULT 'New Chat',

    -- LLM 自动摘要（对话结束后异步生成）
    summary         TEXT         NULL,

    -- 统计
    message_count   INT          NOT NULL DEFAULT 0,
    user_msg_count  INT          NOT NULL DEFAULT 0,
    ai_msg_count    INT          NOT NULL DEFAULT 0,
    total_tokens    INT          NOT NULL DEFAULT 0,   -- 估算：累计 message.token_count

    -- 分类 & 状态
    tags            JSON         NULL,                  -- ["工作","生活","闲聊"]
    is_pinned       TINYINT(1)   NOT NULL DEFAULT 0,
    is_archived     TINYINT(1)   NOT NULL DEFAULT 0,

    -- 记忆提取状态
    last_extracted_at DATETIME   NULL,                  -- 上次从该对话提取记忆的时间

    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_updated (updated_at),
    INDEX idx_pinned  (is_pinned, updated_at),
    INDEX idx_archived (is_archived, updated_at)
);


-- ──────────────────────────
-- 2. 消息
-- ──────────────────────────
CREATE TABLE messages (
    id              CHAR(32)     PRIMARY KEY,
    conversation_id CHAR(32)     NOT NULL,
    role            VARCHAR(16)  NOT NULL,              -- user / assistant / system

    content         TEXT         NOT NULL,

    -- 元数据
    token_count     INT          NOT NULL DEFAULT 0,    -- 估算 token 数（tiktoken）
    source          VARCHAR(16)  NOT NULL DEFAULT 'text',-- voice / text / auto

    -- RAG 状态
    embedding_status VARCHAR(16) NOT NULL DEFAULT 'none',-- none / pending / done / skipped

    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    INDEX idx_conv_time (conversation_id, created_at),
    INDEX idx_embedding  (embedding_status, created_at)
);


-- ──────────────────────────
-- 3. 🆕 长期记忆（已提取的关键事实）
-- ──────────────────────────
CREATE TABLE memories (
    id              CHAR(32)     PRIMARY KEY,

    -- 记忆内容
    content         TEXT         NOT NULL,              -- "用户小明，25岁，喜欢火锅"
    category        VARCHAR(32)  NULL,                  -- identity / preference / event / knowledge
    importance      TINYINT      NOT NULL DEFAULT 5,    -- 1-10，用户标记或自动评分

    -- 来源追溯
    source_conv_id  CHAR(32)     NULL,
    source_msg_ids  JSON         NULL,                  -- ["msg_id_1","msg_id_2"]

    -- ChromaDB 同步
    chroma_id       VARCHAR(64)  NULL,                  -- ChromaDB 里对应的 doc id
    chroma_synced   TINYINT(1)   NOT NULL DEFAULT 0,

    -- 修正 & 淘汰
    is_corrected    TINYINT(1)   NOT NULL DEFAULT 0,    -- 用户手动修正过
    corrected_by    VARCHAR(32)  NULL,                  -- 新增记忆覆盖此条
    is_deleted      TINYINT(1)   NOT NULL DEFAULT 0,

    -- 冲突检测
    conflicts_with  CHAR(32)     NULL,                  -- 指向矛盾的记忆 id

    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_category  (category, importance),
    INDEX idx_chroma    (chroma_synced),
    INDEX idx_importance (importance DESC)
);


-- ──────────────────────────
-- 4. 🆕 记忆提取任务队列
-- ──────────────────────────
CREATE TABLE extraction_tasks (
    id              CHAR(32)     PRIMARY KEY,
    conversation_id CHAR(32)     NOT NULL,
    status          VARCHAR(16)  NOT NULL DEFAULT 'pending', -- pending / processing / done / failed
    extracted_count INT          NULL,                   -- 提取出几条记忆
    error_message   TEXT         NULL,

    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at      DATETIME     NULL,
    completed_at    DATETIME     NULL,

    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    INDEX idx_status (status, created_at)
);
