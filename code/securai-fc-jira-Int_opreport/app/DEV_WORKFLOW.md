# 开发 make_monthly_opreport_*.py 工作流

> 供自己（agent）开发/维护运维月报自动生成脚本时参考。以 `make_monthly_opreport_TSIINFRA.py` 为参考实现（最精炼），UNICYCEN 同构，新增项目（如 TSIWMS）照此套用。

## 0. 一句话目标
读模板 + 数据 CSV → 生成与人工成品逐行一致的运维月报 xlsx，**跑在函数计算(FC)上**。

## 1. 硬约束（不可违背）
- **纯 openpyxl，禁用 xlwings**。`start.py` 是 FC(函数计算) HTTP server，Linux serverless **无 Excel**。xlwings 需 Excel，FC 跑不了。
- 模板 **0 公式** → 不需要公式重算，openpyxl 够用。
- 模板已带合并单元格/格式/边框 → **必须保留**，不能清空重建。

## 2. 关键模式（必守）

### 2.1 `safe_insert_rows(ws, idx, n)` —— 替代 `ws.insert_rows`
openpyxl 3.1.5 的 `insert_rows` 会平移**值/样式**，但**合并单元格只新建平移后的、不删旧的** → 残留重叠合并（如 Q49:R49）= "单元格错乱"。
```python
def safe_insert_rows(ws, idx, n):
    if n <= 0: return
    affected = [(mr.min_col, mr.min_row, mr.max_col, mr.max_row)
                for mr in list(ws.merged_cells.ranges) if mr.max_row >= idx]
    for mr in list(ws.merged_cells.ranges):
        if mr.max_row >= idx: ws.unmerge_cells(str(mr))
    ws.insert_rows(idx, n)           # openpyxl 平移值/样式
    for c1, r1, c2, r2 in affected:
        ws.merge_cells(start_row=(r1+n if r1>=idx else r1), start_column=c1,
                       end_row=r2+n, end_column=c2)
```
**所有 insert 都走这个**，不要直接 `ws.insert_rows`。

### 2.2 告警区（1、総評 / ■検知内容 / ■事象纏め）—— UNICYCEN 方式
- **固定模板位置 + 累积 offset + `safe_insert_rows` + `set_cell`（MS PGothic 10pt 字体）**。
- **不清空重建**。模板表头（1、総評/2、アラート/2-2説明/■検知内容/■事象纏め）保留不动，只填数据。
- 1、総評 概要(R5)：0件留默认 `全体的に安定稼働しております。`；N件清默认→R6插(N-1)行→填 `M/D 、<备注>`。
- ■検知内容(data槽)：0件 `特にございません。`；N件插(N-1)行→`アラート①：YYYY-M-D H:MM <告警简述>`。
- ■事象纏め(data槽)：按 `告警原因` 分组→`アラート①、②：<原因>`；无则 `特にございません。`。
- offset 在三处 insert 后累加，用于 chart 定位。

### 2.3 资源表（第一表 + SVFS第二表）—— 定位填入模板
- **不清空重建**（清空重建会丢全部合并）。改为**搜索 B 列定位主机名**，在其行的合并单元格 **top-left** 填数据。
- 写合并单元格必须写 **top-left**（写 MergedCell 会 AttributeError）。
- 保留模板合并：B:C(主机名)、M:N/P:Q(使用率)、A列(環境)、SVFS D-M(跨行)。
- **系统盘 vs 数据盘**：系统盘 = 盘符 `startswith('/dev/vda')` 或 `C:`；数据盘 = 非系统盘中 `磁盘大小(GB)` 最大者；无数据盘填 `'-'`。
- **磁盘大小用 `math.ceil` 取整**（与人工一致：99.99→100、16384.03→16385）；使用率保留 CSV 原值。
- **第一表**列映射：L=系统盘大小, M=系统盘max(M:N合并), O=数据盘大小, P=数据盘max(P:Q合并)。
- **SVFS 第二表**：主机级 D-M 填首行（跨行合并 top-left，行跨度取自 B:C 合并）；数据盘 N/O/P 每盘一行（盘符顺序按 config 行顺序=人工月报顺序）。C16(ディスク最大拡張容量) 从 config 取（cmd_summary 无此字段）。

### 2.4 課金表
- 定位 `'課金一覧'` 表头，向下扫 C2=主机名 & C4=`月間サブスクリプション` 的行。
- **C7 更新日** = 作成月（today）第一天 → `datetime(today.year, today.month, 1)` + `number_format='yyyy/m/d'`。
- **C11 最新有効期限** = `aliyun_renewal_bill_{target_year}-{target_month:02d}.csv` 的 `资源到期时间`(UTC) 转 JST 日期，按 InstanceId 匹配（ecs InstanceName→InstanceId）。
- 課金 host list 含 SVJS01（不在资源表，模板静态）。

### 2.5 section 7 月份占位符
- 模板 `7-1 M月の作業状況` / `7-2 N月の作業予定` → 全表搜 `M月`/`N月` 替换为 `{target_month}月`/`{next_month}月`。

### 2.6 图表
- `_create_alert_chart`：推移 sheet 引用的 BarChart，锚点 `A{alert_count_row+1+offset}`。保存后用 `_fix_chart_sizes_in_xlsx` 修 anchor ext 尺寸（openpyxl 保存时 ext 不正确）。

## 3. 数据源
| 文件 | 用途 |
|------|------|
| `jira_get_alert.csv` | 告警（Project Key 过滤，排除 备注=不记录月报，+0800→JST） |
| `aliyun_op_cmd_summary.csv` | CPU/Mem/Disk 月次汇总。**列名 `磁盘最大使用率（%)` 是全角`）`** |
| `aliyun_ecs_op.csv` | ECS 实例（InstanceId/InstanceName/CPU核/Memory(MB)/标签） |
| `aliyun_renewal_bill_{YYYY-MM}.csv` | 課金 C11 最新有効期限（资源到期时间） |
| `jira_get_op_account.csv` | 账号→Project Key 映射（标签过滤） |
| `config/opreport_names.csv` | Project Key→公司名/系统名 |
| `config/opreport_hosts.csv` | InstanceId→显示名/環境（SVFS 不在此，需特殊处理） |
| `config/opreport_svfs_disk_expand.csv` | SVFS C16 最大拡張容量 + 数据盘顺序（人工记录） |

## 4. 开发流程（标准工作流）
1. **对比成品 vs 自动**：`code/jira/26年6月/<成品>.xlsx` vs `code/jira/monthly_report/<PROJ>/...xlsx`。逐行 diff 総評 sheet（结构 col1-2、关键数据、合并、填充）。
2. **定位差异**，分类：结构错 / 数据来源缺 / 格式(合并/填充/字体)丢 / 数值漂移(CSV刷新,可接受)。
3. **数据来源不明 → 问用户**（用户原话："如果有地方你不知道数据来源 问我"）。用 AskUserQuestion 给选项。
4. **实现**：遵循第 2 节模式。改 `populate_summary` + 辅助函数。新增项目常量(LAYOUT/PROJECT_KEY/模板名)对齐**修改后模板**的固定行号。
5. **验证**（见第 6 节清单）。
6. **记忆**：关键决策/坑写入 memory（`opreport-*.md`），更新 MEMORY.md 索引。

## 5. 常见坑
- `ws.insert_rows` 合并错乱 → `safe_insert_rows`。
- clear+rebuild 丢合并 → 定位填入模板。
- 写合并单元格非 top-left → AttributeError。
- theme 色读取：`fg.rgb` 在 theme 色时抛 "Values must be of type str" → 用 `getattr` + try/except，看 `fg.theme`/`fg.tint`。`patternType=None` = 无填充。
- openpyxl 读日期：manual 可能存 int 序列号(46204)+General 格式，auto 写 datetime+`yyyy/m/d` —— 同一日期不同表示，视觉都对。
- SVFS 盘符顺序：用 config 行顺序（=人工月报顺序），不要用 cmd_summary 顺序（quicksort 会乱）。
- 条件格式填充：openpyxl `cell.fill` 不显示条件格式，要查 `ws.conditional_formatting`（本模板无）。
- 模板被改过：用户会直接改模板（加默认 slot、整体下移）。开发前**先 dump 当前模板结构**（行/合并），不要用记忆里的旧行号。

## 6. 验证清单
- [ ] `python -m py_compile` 通过
- [ ] 脚本运行无异常，生成报表
- [ ] 総評 结构(col1-2) vs 成品逐行一致（除已知差异：CSV数值漂移、用户决定留空项如 7-1 作業況）
- [ ] 合并数 auto = template（定位填入模板后应相等）
- [ ] 无残留合并（safe_insert_rows 后无重叠）
- [ ] 課金 C7=作成月第一天、C11=bill到期时间(JST)
- [ ] section 7 M月/N月 已替换
- [ ] 图表存在(1个)
- [ ] 告警区字体 = MS PGothic 10pt

## 7. 项目差异速查
| 项 | TSIINFRA | UNICYCEN |
|----|----------|----------|
| sheet 名 | 総評1-8 | 総評 |
| 资源表 | 第一表(tcp+SVFR/SVVS) + SVFS第二表(多盘) | CPU/Mem表(Proxy001/002) + CEN帯域 |
| 特殊 | SVFS 多盘 + C16 config + C11 bill | CEN帯域手動、Proxy表示名映射 |
| 模板 | 运维月报模板_【株式会社TSI様向け】インフラ月次報告書.xlsx | 运维月报模板_【ユニ・チャーム株式会社様】CENサービス運用報告書.xlsx |

新增项目：复制 TSIINFRA 脚本 → 改 PROJECT_KEY/模板名/LAYOUT 行号 → 按成品 diff 调资源表逻辑。
