-- 博主库给人用的表结构与列。这份文本原样进查库大模型的材料，所以只列有用的列。
--
-- 不列出的列，以及为什么不列（都是量出来的，不是猜的）：
--   credit_level   常量：470,398 行是 2，1 行是 3
--   is_public      常量：全部 1
--   data_date      常量：全部 2026-09-14
--   batch_id       常量：全部 1
--   imported_at    常量：全部 2026-09-13 18:02:45
--   extra_json     全空：470,399 行全为 NULL/空串
--   src_row_index  只是 1..470,399 的行序号，和 id 同序，没有筛选价值
--   keywords       派生文本，且不是 raw_categories 的简单拼接（按逗号换空格对不上任何一行）
--   summary_text   派生文本，把昵称、粉丝、类目、地区重新拼成一句话
--
-- 注意：不列出来只影响模型看不看得见，挡不住模型写 SELECT *。
-- 执行器会把 SELECT * 的全部列原样回填，这些列照样会进回填。

CREATE TABLE xhs_creator (
  id             INTEGER,  -- 按粉丝降序的排序序号。不是身份键，不要用它认人
  nickname       TEXT,     -- 昵称。重名严重（11,623 种重名），不能当身份
  xhs_id         TEXT,     -- 小红书号，身份的唯一定位。470,345 行有值且零重复
  gender         TEXT,     -- 女 / 男 / 未知。37 行是"未知"，1 行为空
  followers      INTEGER,  -- 粉丝数，无空值。p50=4479 / p75=12940 / p95=79684
  province       TEXT,     -- 省份。1 行为空
  city           TEXT,     -- 城市。105,154 行为空（22.4%），按城市筛会漏掉这批人
  email          TEXT,     -- 联系邮箱。99,544 行非空。含全角符号，比较前要先归一化
  raw_categories TEXT     -- 类目原文，逗号分隔。和 xhs_creator_category 逐行对应，无例外
);

CREATE TABLE xhs_creator_category (
  creator_id     INTEGER,  -- 对应 xhs_creator.id（不是 xhs_id）
  category_name  TEXT      -- 一条类目名。一个账号可以有多条，共 516,196 行
);

-- 这张表在库里存在，但 0 行：它是类目别名（口语说法 → 类目名）的落点。
-- CREATE TABLE dict_category_alias (category_name TEXT, alias TEXT);
